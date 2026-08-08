"""Registration, sign-in, lockout, two-factor and secret encryption."""

from __future__ import annotations

import pyotp
import pytest
from flask import Flask
from flask.testing import FlaskClient
from freezegun import freeze_time

from massingbill.errors import ConflictError, ValidationError
from massingbill.extensions import db
from massingbill.models import Role
from massingbill.services import accounts, mfa
from massingbill.services.accounts import SignInOutcome
from massingbill.services.crypto import DecryptionError, SecretBox
from tests.factories import PASSWORD, make_tenant, make_user, sign_in

# ── Registration ────────────────────────────────────────────────────────────


def test_registering_creates_a_user_an_organization_and_an_owner(
    client: FlaskClient, app: Flask
) -> None:
    response = client.post(
        "/auth/register",
        data={
            "name": "Dana Reyes",
            "email": "dana@example.com",
            "password": PASSWORD,
            "organization_name": "Reyes Builders",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    user = accounts.get_user_by_email("dana@example.com")
    assert user is not None

    memberships = accounts.memberships_for(user)
    assert len(memberships) == 1
    assert memberships[0].role == Role.OWNER


def test_email_addresses_are_stored_lowercase(app: Flask) -> None:
    user = accounts.create_user("  Mixed.Case@Example.COM ", PASSWORD)
    assert user.email == "mixed.case@example.com"


def test_a_duplicate_address_is_refused(app: Flask) -> None:
    accounts.create_user("dup@example.com", PASSWORD)
    with pytest.raises(ConflictError, match="already exists"):
        accounts.create_user("dup@example.com", PASSWORD)


def test_short_passwords_are_refused(app: Flask) -> None:
    with pytest.raises(ValidationError, match="at least 12"):
        accounts.create_user("short@example.com", "tooshort")


def test_organization_slugs_are_disambiguated(app: Flask) -> None:
    first = make_user("a@example.com")
    second = make_user("b@example.com")

    one = accounts.create_organization("Acme Construction", first)
    two = accounts.create_organization("Acme Construction", second)

    assert one.slug != two.slug


# ── Sign-in ─────────────────────────────────────────────────────────────────


def test_correct_credentials_succeed(app: Flask) -> None:
    user = make_user("pm@example.com")
    result = accounts.attempt_sign_in("pm@example.com", PASSWORD)

    assert result.ok
    assert result.user is not None
    assert result.user.id == user.id


def test_an_unknown_address_and_a_wrong_password_are_indistinguishable(app: Flask) -> None:
    make_user("known@example.com")

    unknown = accounts.attempt_sign_in("nobody@example.com", PASSWORD)
    wrong = accounts.attempt_sign_in("known@example.com", "not-the-password")

    assert unknown.outcome == SignInOutcome.BAD_CREDENTIALS
    assert wrong.outcome == SignInOutcome.BAD_CREDENTIALS


def test_the_sign_in_page_never_says_which_part_was_wrong(client: FlaskClient, app: Flask) -> None:
    make_user("known@example.com")
    response = client.post(
        "/auth/sign-in", data={"email": "known@example.com", "password": "wrong-password"}
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 401
    assert "not valid" in body
    assert "no such" not in body.lower()
    assert "password is" not in body.lower()


def test_repeated_failures_lock_the_account(app: Flask) -> None:
    make_user("target@example.com")

    for _ in range(accounts.MAX_FAILED_LOGINS):
        accounts.attempt_sign_in("target@example.com", "wrong")

    result = accounts.attempt_sign_in("target@example.com", PASSWORD)
    assert result.outcome == SignInOutcome.LOCKED


def test_a_lockout_expires(app: Flask) -> None:
    make_user("target@example.com")

    with freeze_time("2026-08-08 12:00:00"):
        for _ in range(accounts.MAX_FAILED_LOGINS):
            accounts.attempt_sign_in("target@example.com", "wrong")
        assert accounts.attempt_sign_in("target@example.com", PASSWORD).outcome == (
            SignInOutcome.LOCKED
        )

    with freeze_time("2026-08-08 12:16:00"):
        assert accounts.attempt_sign_in("target@example.com", PASSWORD).ok


def test_a_successful_sign_in_clears_the_failure_counter(app: Flask) -> None:
    user = make_user("target@example.com")

    accounts.attempt_sign_in("target@example.com", "wrong")
    accounts.attempt_sign_in("target@example.com", "wrong")
    assert user.failed_login_count == 2

    accounts.attempt_sign_in("target@example.com", PASSWORD)
    assert user.failed_login_count == 0


def test_an_inactive_account_cannot_sign_in(app: Flask) -> None:
    user = make_user("gone@example.com")
    user.is_active = False
    db.session.flush()

    assert accounts.attempt_sign_in("gone@example.com", PASSWORD).outcome == (
        SignInOutcome.INACTIVE
    )


def test_signing_out_clears_the_session(client: FlaskClient, app: Flask) -> None:
    tenant = make_tenant("acme")
    sign_in(client, tenant.user(Role.OWNER))
    assert client.get("/projects").status_code == 200

    client.post("/auth/sign-out")
    assert client.get("/projects").status_code == 302


# ── Membership rules ────────────────────────────────────────────────────────


def test_the_last_owner_cannot_be_demoted(app: Flask) -> None:
    owner = make_user("solo@example.com")
    organization = accounts.create_organization("Solo Builders", owner)
    membership = accounts.memberships_for(owner)[0]

    with pytest.raises(ConflictError, match="only owner"):
        accounts.change_member_role(membership, Role.VIEWER, actor=owner)

    assert organization is not None


def test_the_last_owner_cannot_be_removed(app: Flask) -> None:
    owner = make_user("solo@example.com")
    accounts.create_organization("Solo Builders", owner)
    membership = accounts.memberships_for(owner)[0]

    with pytest.raises(ConflictError, match="only owner"):
        accounts.remove_member(membership, actor=owner)


def test_an_owner_can_be_demoted_once_there_is_another(app: Flask) -> None:
    first = make_user("first@example.com")
    organization = accounts.create_organization("Two Owners", first)

    second = make_user("second@example.com")
    accounts.add_member(organization, second, Role.OWNER, actor=first)

    membership = accounts.memberships_for(first)[0]
    accounts.change_member_role(membership, Role.PM, actor=first)
    assert membership.role == Role.PM


def test_adding_the_same_member_twice_is_refused(app: Flask) -> None:
    owner = make_user("owner@example.com")
    organization = accounts.create_organization("Acme", owner)
    other = make_user("other@example.com")

    accounts.add_member(organization, other, Role.PM, actor=owner)
    with pytest.raises(ConflictError, match="already a member"):
        accounts.add_member(organization, other, Role.VIEWER, actor=owner)


# ── Two-factor ──────────────────────────────────────────────────────────────


def test_enrolment_stores_the_secret_only_after_a_valid_code(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, uri = mfa.begin_enrolment(user)

    assert user.totp_secret is None, "a mis-scanned QR must not lock the user out"
    assert "otpauth://totp/" in uri

    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())
    assert user.mfa_enabled


def test_a_wrong_enrolment_code_is_refused(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, _ = mfa.begin_enrolment(user)

    with pytest.raises(ValidationError, match="not valid"):
        mfa.confirm_enrolment(user, secret, "000000")
    assert not user.mfa_enabled


def test_the_stored_secret_is_encrypted(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())

    assert user.totp_secret is not None
    assert secret not in user.totp_secret
    assert user.totp_secret.startswith("v1:")
    assert mfa.secret_for(user) == secret


def test_a_code_verifies_and_a_wrong_one_does_not(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())

    assert mfa.verify_user_code(user, pyotp.TOTP(secret).now())
    assert not mfa.verify_user_code(user, "123456")
    assert not mfa.verify_user_code(user, "not-a-code")


def test_sign_in_stops_for_a_code_when_two_factor_is_on(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())

    result = accounts.attempt_sign_in("mfa@example.com", PASSWORD)
    assert result.outcome == SignInOutcome.NEEDS_MFA


def test_the_full_two_factor_sign_in_flow(client: FlaskClient, app: Flask) -> None:
    tenant = make_tenant("acme")
    user = tenant.user(Role.OWNER)

    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())
    db.session.commit()

    first = client.post("/auth/sign-in", data={"email": user.email, "password": PASSWORD})
    assert first.status_code == 302
    assert "/auth/mfa" in first.headers["Location"]

    # Not logged in yet.
    assert client.get("/projects").status_code == 302

    second = client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now()})
    assert second.status_code == 302
    assert client.get("/projects").status_code == 200


def test_the_enrolment_qr_is_an_inline_svg_data_uri(app: Flask) -> None:
    """CSP is default-src 'self' with img-src allowing data:. A chart API or a
    CDN image would be blocked, and the secret would leave the machine."""
    user = make_user("mfa@example.com")
    _, uri = mfa.begin_enrolment(user)
    data_uri = mfa.qr_data_uri(uri)

    assert data_uri.startswith("data:image/svg+xml;base64,")
    assert "<svg" in mfa.qr_svg(uri)


def test_disabling_two_factor_clears_the_secret(app: Flask) -> None:
    user = make_user("mfa@example.com")
    secret, _ = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, secret, pyotp.TOTP(secret).now())

    mfa.disable(user)
    assert not user.mfa_enabled
    assert user.totp_secret is None


# ── Secret encryption ───────────────────────────────────────────────────────


def test_encryption_round_trip() -> None:
    box = SecretBox("a-key-for-testing-only")
    assert box.decrypt(box.encrypt("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"


def test_ciphertext_is_not_deterministic() -> None:
    """A repeated nonce with AES-GCM is catastrophic; a fresh one every time
    also means two users with the same seed do not share a ciphertext."""
    box = SecretBox("a-key-for-testing-only")
    assert box.encrypt("same") != box.encrypt("same")


def test_the_wrong_key_cannot_decrypt() -> None:
    sealed = SecretBox("key-one").encrypt("secret")
    with pytest.raises(DecryptionError, match="key is wrong"):
        SecretBox("key-two").decrypt(sealed)


def test_tampered_ciphertext_is_rejected() -> None:
    box = SecretBox("a-key-for-testing-only")
    sealed = box.encrypt("secret")
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")

    with pytest.raises(DecryptionError):
        box.decrypt(tampered)


@pytest.mark.parametrize("bad", ["", "nope", "v2:abc", "v1:", "v1:!!!not-base64!!!", "v1:AAAA"])
def test_malformed_ciphertext_is_rejected(bad: str) -> None:
    with pytest.raises(DecryptionError):
        SecretBox("a-key-for-testing-only").decrypt(bad)


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        SecretBox("")
