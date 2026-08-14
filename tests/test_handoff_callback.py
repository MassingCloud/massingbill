"""The massing.cloud handoff, from URL to session.

`test_massing_handoff.py` covers the cryptography. This covers the half that has
a database: single use, who the assertion resolves to, and the fact that every
refusal looks identical from outside.

Driven through the route rather than the service, because the things most likely
to break are the wiring — a missing config gate, a commit that never happens, a
session that is not actually established.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.extensions import db
from massingbill.models import Role, SpentHandoff
from tests.factories import Tenant, make_tenant

pytestmark = pytest.mark.adapter

pytest.importorskip(
    "massingbill.services.identity.massing_handoff",
    reason="the massing handoff adapter is not installed",
)

SECRET = "a-shared-secret-of-reasonable-length"
CALLBACK = "/auth/massing/callback"


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    app.config["MASSINGBILL_MASSING_SHARED_SECRET"] = SECRET
    db.session.commit()
    return built


def mint(tenant: Tenant, *, secret: str = SECRET, **overrides: Any) -> str:
    """An assertion for this tenant's PM, as the bridge would mint it."""
    now = int(time.time())
    user = tenant.user(Role.PM)
    claims: dict[str, Any] = {
        "sub": "42",
        "email": user.email,
        "name": "Pat M",
        "org": tenant.organization.id,
        "iat": now,
        "exp": now + 60,
        "jti": f"jti-{now}-{overrides.pop('nonce', '0')}",
    }
    claims.update(overrides)

    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(
                SECRET.encode() if secret is SECRET else secret.encode(),
                payload.encode(),
                hashlib.sha256,
            ).digest()
        )
        .decode()
        .rstrip("=")
    )
    return f"{payload}.{signature}"


def signed_in(client: FlaskClient) -> bool:
    """True when the session actually reaches an authenticated page."""
    return client.get("/projects", follow_redirects=False).status_code == 200


# ── The happy path ──────────────────────────────────────────────────────────


def test_a_valid_assertion_signs_the_member_in(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)

    assert response.status_code == 200
    assert signed_in(client)


def test_the_asserted_organization_becomes_the_active_one(
    client: FlaskClient, tenant: Tenant
) -> None:
    """Otherwise somebody arrives signed in with no tenant selected and every
    project page 404s for reasons nobody can see."""
    response = client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)

    assert tenant.organization.name.encode() in response.data


def test_using_the_link_records_it_as_spent(client: FlaskClient, tenant: Tenant) -> None:
    client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)

    assert db.session.query(SpentHandoff).count() == 1


# ── Single use ──────────────────────────────────────────────────────────────


def test_the_same_link_cannot_be_used_twice(
    client: FlaskClient, tenant: Tenant, app: Flask
) -> None:
    """The whole reason the spent table exists. A captured URL is worth one
    redirect, and only if nobody used it first.

    The second attempt uses a **fresh client**, which is the shape of the real
    attack: somebody who is not the original user replaying a URL out of a proxy
    log. Reusing the same client would leave the first session in place and the
    assertion would look accepted when it had in fact been refused -- which is
    exactly what this test did on its first run.
    """
    assertion = mint(tenant)

    client.get(f"{CALLBACK}?assertion={assertion}", follow_redirects=True)
    assert signed_in(client), "the first use should have worked"

    attacker = app.test_client()
    attacker.get(f"{CALLBACK}?assertion={assertion}", follow_redirects=True)

    assert not signed_in(attacker)


def test_a_replay_leaves_exactly_one_spent_row(client: FlaskClient, tenant: Tenant) -> None:
    assertion = mint(tenant)

    for _ in range(3):
        client.get(f"{CALLBACK}?assertion={assertion}", follow_redirects=True)

    assert db.session.query(SpentHandoff).count() == 1


def test_an_assertion_with_no_jti_is_refused(client: FlaskClient, tenant: Tenant) -> None:
    """Without one, single use cannot be enforced at all, so the safe reading
    is that it is not a valid assertion rather than that it is unlimited."""
    response = client.get(f"{CALLBACK}?assertion={mint(tenant, jti='')}", follow_redirects=True)

    assert not signed_in(client)
    assert b"not valid" in response.data


# ── Who it resolves to ──────────────────────────────────────────────────────


def test_an_unknown_email_does_not_create_an_account(client: FlaskClient, tenant: Tenant) -> None:
    """A valid assertion says massing.cloud believes this person is entitled,
    not "give this person a login". Otherwise one leaked secret mints accounts
    in every tenant."""
    from massingbill.models import User

    before = db.session.query(User).count()

    client.get(
        f"{CALLBACK}?assertion={mint(tenant, email='stranger@nowhere.example')}",
        follow_redirects=True,
    )

    assert not signed_in(client)
    assert db.session.query(User).count() == before


def test_a_real_user_who_is_not_a_member_is_refused(
    client: FlaskClient, tenant: Tenant, app: Flask
) -> None:
    """The account exists, the signature is good, and they still do not belong
    to the organization the assertion names."""
    other = make_tenant("rival")
    db.session.commit()

    client.get(
        f"{CALLBACK}?assertion={mint(tenant, email=other.user(Role.PM).email)}",
        follow_redirects=True,
    )

    assert not signed_in(client)


def test_an_unknown_organization_is_refused(client: FlaskClient, tenant: Tenant) -> None:
    client.get(f"{CALLBACK}?assertion={mint(tenant, org='no-such-org')}", follow_redirects=True)

    assert not signed_in(client)


# ── The cryptographic refusals still refuse, through the route ──────────────


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("forged", {"secret": "wrong-secret"}),
        ("expired", {"iat": int(time.time()) - 300, "exp": int(time.time()) - 200}),
        ("future", {"iat": int(time.time()) + 600, "exp": int(time.time()) + 900}),
    ],
)
def test_a_bad_assertion_is_refused(
    client: FlaskClient, tenant: Tenant, label: str, kwargs: dict[str, Any]
) -> None:
    client.get(f"{CALLBACK}?assertion={mint(tenant, **kwargs)}", follow_redirects=True)
    assert not signed_in(client), label


def test_a_missing_assertion_is_refused(client: FlaskClient, tenant: Tenant) -> None:
    client.get(CALLBACK, follow_redirects=True)
    assert not signed_in(client)


def test_every_refusal_says_the_same_thing(client: FlaskClient, tenant: Tenant) -> None:
    """Whether the signature failed, the link expired, or the account does not
    exist must be indistinguishable from outside. Distinguishing them is free
    reconnaissance."""
    seen = set()

    for assertion in (
        mint(tenant, secret="wrong-secret", nonce="a"),
        mint(tenant, iat=int(time.time()) - 300, exp=int(time.time()) - 200, nonce="b"),
        mint(tenant, email="stranger@nowhere.example", nonce="c"),
        mint(tenant, org="no-such-org", nonce="d"),
    ):
        response = client.get(f"{CALLBACK}?assertion={assertion}", follow_redirects=True)
        body = response.data
        seen.add(b"not valid" in body)

    assert seen == {True}, "the refusals differ from one another"


# ── The endpoint does not exist unless it is configured ─────────────────────


def test_the_route_404s_when_no_secret_is_configured(
    client: FlaskClient, app: Flask, tenant: Tenant
) -> None:
    """A standalone install must not advertise an endpoint it cannot honour,
    and a scanner should learn nothing from asking."""
    app.config["MASSINGBILL_MASSING_SHARED_SECRET"] = ""

    assert client.get(f"{CALLBACK}?assertion={mint(tenant)}").status_code == 404


# ── Housekeeping ────────────────────────────────────────────────────────────


def test_spent_records_are_prunable(app: Flask) -> None:
    """They are worthless once no assertion they describe could still be valid,
    and nothing else ever deletes them."""
    from datetime import timedelta

    from massingbill.models.base import utcnow
    from massingbill.services import handoff as handoff_service

    db.session.add(SpentHandoff(jti="old", used_at=utcnow() - timedelta(days=2)))
    db.session.add(SpentHandoff(jti="fresh", used_at=utcnow()))
    db.session.commit()

    assert handoff_service.prune() == 1
    assert db.session.query(SpentHandoff).count() == 1


# ── A handoff is one factor, not two ────────────────────────────────────────
#
# The original implementation called login_user() directly, so a user with TOTP
# enrolled was signed in without it. The assertion carries no acr, amr or AAL
# claim (NIST SP 800-63C; RFC 8176), so there is nothing in it on which to
# conclude a second factor was used.


def enrol_mfa(user) -> str:
    """Enrol TOTP through the real flow, and return the secret.

    Not by setting a flag: `mfa_enabled` is derived from a confirmed secret, so
    faking it would test a state the application cannot actually be in.
    """
    import pyotp

    from massingbill.services import mfa

    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())
    db.session.commit()
    return secret


def test_a_user_with_totp_is_challenged_rather_than_signed_in(
    client: FlaskClient, tenant: Tenant
) -> None:
    """The finding this section exists for. A valid assertion must not be a way
    around a second factor the user deliberately enrolled."""
    enrol_mfa(tenant.user(Role.PM))

    client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=False)

    assert not signed_in(client), "TOTP was bypassed by the handoff"


def test_the_challenge_is_the_same_one_the_password_path_uses(
    client: FlaskClient, tenant: Tenant
) -> None:
    enrol_mfa(tenant.user(Role.PM))

    response = client.get(f"{CALLBACK}?assertion={mint(tenant, nonce='m')}")

    assert response.status_code in (302, 303)
    assert "/auth/mfa" in response.headers["Location"]


def test_completing_the_challenge_lands_in_the_asserted_organization(
    client: FlaskClient, tenant: Tenant, app: Flask
) -> None:
    """`_finish_login` otherwise picks the *first* membership. A user in two
    organizations would verify their code and arrive in the wrong tenant --
    which on this product means the wrong contractor's pay applications."""
    import pyotp

    from massingbill.services import accounts

    user = tenant.user(Role.PM)
    other = make_tenant("rival")
    accounts.add_member(other.organization, user, Role.ACCOUNTANT)
    secret = enrol_mfa(user)

    client.get(f"{CALLBACK}?assertion={mint(tenant, nonce='x')}", follow_redirects=False)

    response = client.post(
        "/auth/mfa", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=True
    )

    assert response.status_code == 200
    assert tenant.organization.name.encode() in response.data


# ── Account state is one policy, checked by every entry point ───────────────


def test_a_locked_account_cannot_sign_in_through_the_bridge(
    client: FlaskClient, tenant: Tenant
) -> None:
    """Lockout that the password path enforces must not be walkable around by
    arriving through the bridge instead."""
    from datetime import timedelta

    from massingbill.models.base import utcnow

    user = tenant.user(Role.PM)
    user.locked_until = utcnow() + timedelta(hours=1)
    db.session.commit()

    client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)

    assert not signed_in(client)


def test_an_inactive_account_cannot_sign_in_through_the_bridge(
    client: FlaskClient, tenant: Tenant
) -> None:
    user = tenant.user(Role.PM)
    user.is_active = False
    db.session.commit()

    client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)

    assert not signed_in(client)


def test_a_successful_handoff_records_the_sign_in(client: FlaskClient, tenant: Tenant) -> None:
    """`complete_sign_in` was being skipped, so last_login_at never moved for
    bridge users and stale-account reporting under-counted them."""
    user = tenant.user(Role.PM)
    user.last_login_at = None
    db.session.commit()

    client.get(f"{CALLBACK}?assertion={mint(tenant)}", follow_redirects=True)
    db.session.refresh(user)

    assert user.last_login_at is not None


# ── Operator-facing failures, which are a different problem ─────────────────


def test_a_missing_adapter_is_reported_as_an_operator_problem(
    client: FlaskClient, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured but not installed is not "your link is bad". It used to
    surface as one, because `accept()` raised a bare ImportError that the
    generic handler swallowed, and the runbook's list of causes to grep for did
    not include it."""
    import sys

    from massingbill.errors import AdapterUnavailableError
    from massingbill.services import handoff as handoff_service

    # Both halves are needed to make an already-imported submodule look absent:
    # `from package import submodule` finds it as an attribute of the package
    # before it ever consults sys.modules, and a None entry in sys.modules is
    # what makes the machinery raise ImportError rather than re-import it.
    # Patching __import__ does neither, which is why the first attempt at this
    # test passed against broken code.
    from massingbill.services import identity

    monkeypatch.delattr(identity, "massing_handoff", raising=False)
    monkeypatch.setitem(sys.modules, "massingbill.services.identity.massing_handoff", None)

    with pytest.raises(AdapterUnavailableError, match="pip install"):
        handoff_service.accept(mint(tenant), secret=SECRET)


def test_the_next_parameter_cannot_leave_the_site(client: FlaskClient, tenant: Tenant) -> None:
    """A sign-in that forwards anywhere is a phishing primitive: the link really
    is this application, and the redirect happens after a genuine login."""
    from massingbill.blueprints.auth import _safe_next

    for hostile in ("https://evil.example/x", "//evil.example/x", "http://evil.example"):
        assert _safe_next(hostile) is None, hostile

    assert _safe_next("/projects") == "/projects"
    assert _safe_next(None) is None
