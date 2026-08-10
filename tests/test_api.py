"""The REST API: authentication, scoping, tenant isolation and the envelope.

The cross-tenant probes matter more here than anywhere else in the suite. A
browser session at least has a human behind it; an API key is handed to a
script that will be pointed at the wrong organization eventually, and the only
thing standing between that and a leak is that every query goes through
``scoped()``.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.extensions import db
from massingbill.models import ApiKey
from massingbill.services import apikeys
from massingbill.services.rbac import (
    ALL_PERMISSIONS,
    APPLICATION_READ,
    APPLICATION_SUBMIT,
    PROJECT_READ,
    READ_PERMISSIONS,
)
from tests.factories import Tenant, add_balanced_lines, make_tenant

BASE = "/api/massingbill/v1"


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    add_balanced_lines(built)
    return built


@pytest.fixture
def other_tenant(app: Flask) -> Tenant:
    built = make_tenant("rival")
    add_balanced_lines(built)
    return built


def mint(tenant: Tenant, scopes: object = None) -> str:
    minted = apikeys.mint(tenant.organization, name="test key", scopes=scopes)
    db.session.commit()
    return minted.token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Authentication ──────────────────────────────────────────────────────────


def test_a_valid_key_reaches_the_api(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get(f"{BASE}/status", headers=auth(mint(tenant)))

    assert response.status_code == 200
    assert response.json["data"]["organization_id"] == tenant.organization.id


def test_the_x_api_key_header_works_too(client: FlaskClient, tenant: Tenant) -> None:
    """massing accepts both, so we accept both."""
    response = client.get(f"{BASE}/status", headers={"X-Api-Key": mint(tenant)})
    assert response.status_code == 200


def test_no_key_is_401(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get(f"{BASE}/status")
    assert response.status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "not-a-token",
        "mbil_short",
        "mbil_deadbeefdeadbeefdeadbeef_wrongsecret",
        "wrongprefix_deadbeefdeadbeefdeadbeef_secret",
    ],
)
def test_every_bad_token_gets_the_same_answer(
    client: FlaskClient, tenant: Tenant, token: str
) -> None:
    """One message for malformed, unknown and wrong-secret alike.

    Distinguishing them tells an attacker which half of a guess was right.
    """
    mint(tenant)  # a real key exists, so "none exist" is not why this fails
    response = client.get(f"{BASE}/status", headers=auth(token))

    assert response.status_code == 401
    assert response.json["message"] == "That API key is not valid."


def test_a_revoked_key_stops_working(client: FlaskClient, tenant: Tenant) -> None:
    minted = apikeys.mint(tenant.organization, name="doomed")
    db.session.commit()
    assert client.get(f"{BASE}/status", headers=auth(minted.token)).status_code == 200

    apikeys.revoke(minted.key)
    db.session.commit()

    assert client.get(f"{BASE}/status", headers=auth(minted.token)).status_code == 401


def test_the_secret_is_never_stored(tenant: Tenant) -> None:
    minted = apikeys.mint(tenant.organization, name="k")
    db.session.commit()

    secret = minted.token.split("_")[-1]
    stored = db.session.get(ApiKey, minted.key.id)
    assert stored is not None
    assert secret not in stored.secret_hash
    assert secret != stored.secret_hash
    # And nothing anywhere else on the row carries it either.
    assert secret not in repr({c.name: getattr(stored, c.name) for c in stored.__table__.columns})


def test_a_secret_containing_an_underscore_still_authenticates(
    client: FlaskClient, tenant: Tenant
) -> None:
    """``token_urlsafe`` emits ``_``, and ``_`` is the token's own delimiter.

    Splitting without a bound tore apart about half of every key ever issued,
    at random, with no way to tell a mangled key from a wrong one. Pinned with
    a constructed token rather than a minted one, because a minted one
    reproduces it only half the time -- which is how it survived being written.
    """
    minted = apikeys.mint(tenant.organization, name="k")
    db.session.commit()

    public_id, secret = apikeys.parse(minted.token)
    assert "_" not in public_id, "the public id must never contain the delimiter"

    forced = f"mbil_{public_id}_pre_fix_ed{secret}"
    assert apikeys.parse(forced) == (public_id, f"pre_fix_ed{secret}")


def test_using_a_key_records_that_it_was_used(client: FlaskClient, tenant: Tenant) -> None:
    minted = apikeys.mint(tenant.organization, name="k")
    db.session.commit()
    assert minted.key.last_used_at is None

    client.get(f"{BASE}/status", headers=auth(minted.token))

    db.session.expire_all()
    assert db.session.get(ApiKey, minted.key.id).last_used_at is not None


# ── Scopes ──────────────────────────────────────────────────────────────────


def test_a_key_defaults_to_read_only(tenant: Tenant) -> None:
    """A key minted without thinking about scopes must not be able to submit."""
    minted = apikeys.mint(tenant.organization, name="thoughtless")
    assert minted.key.scope_set == READ_PERMISSIONS
    assert APPLICATION_SUBMIT not in minted.key.scope_set


def test_a_scope_it_does_not_hold_is_403(client: FlaskClient, tenant: Tenant) -> None:
    token = mint(tenant, scopes={PROJECT_READ})
    response = client.get(f"{BASE}/applications", headers=auth(token))

    assert response.status_code == 403
    assert "scope" in response.json["message"]


def test_an_unknown_scope_is_refused_at_mint_time(tenant: Tenant) -> None:
    from massingbill.errors import ValidationError

    with pytest.raises(ValidationError, match="Unknown scope"):
        apikeys.mint(tenant.organization, name="bad", scopes={"application:destroy"})


def test_scopes_come_from_the_role_vocabulary(tenant: Tenant) -> None:
    """A key can never express an authority the role system has no word for."""
    minted = apikeys.mint(tenant.organization, name="full", scopes=ALL_PERMISSIONS)
    assert minted.key.scope_set <= ALL_PERMISSIONS


# ── Tenant isolation ────────────────────────────────────────────────────────


def test_a_key_sees_only_its_own_organizations_projects(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    response = client.get(f"{BASE}/projects", headers=auth(mint(tenant)))

    ids = {row["id"] for row in response.json["data"]}
    assert tenant.project.id in ids
    assert other_tenant.project.id not in ids


def test_another_tenants_project_is_404_not_403(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """404, because 403 would confirm the id is real."""
    response = client.get(f"{BASE}/projects/{other_tenant.project.id}", headers=auth(mint(tenant)))
    assert response.status_code == 404


def test_no_api_route_leaks_across_tenants(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    token = mint(tenant, scopes=ALL_PERMISSIONS)
    stranger = other_tenant.project.id

    for url in (
        f"{BASE}/projects/{stranger}",
        f"{BASE}/projects/{stranger}/schedule-of-values",
    ):
        assert client.get(url, headers=auth(token)).status_code == 404, url


def test_one_requests_key_does_not_authorize_the_next(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The principal lives on ``g``, which outlives a request unless cleared."""
    client.get(f"{BASE}/projects", headers=auth(mint(tenant)))

    response = client.get(f"{BASE}/projects")
    assert response.status_code == 401


# ── The envelope ────────────────────────────────────────────────────────────


def test_money_is_returned_as_cents_and_never_as_a_float(
    client: FlaskClient, tenant: Tenant
) -> None:
    response = client.get(f"{BASE}/projects/{tenant.project.id}", headers=auth(mint(tenant)))
    amount = response.json["data"]["contracts"][0]["original_contract_sum"]

    assert isinstance(amount["cents"], int)
    assert isinstance(amount["amount"], str)
    assert amount["currency"] == "USD"


def test_lists_are_paginated(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get(f"{BASE}/projects?per_page=1", headers=auth(mint(tenant)))

    meta = response.json["meta"]
    assert meta["per_page"] == 1
    assert meta["page"] == 1
    assert "total" in meta


def test_an_absurd_page_size_is_capped_not_rejected(client: FlaskClient, tenant: Tenant) -> None:
    """An integration should degrade to slower, never to broken."""
    response = client.get(f"{BASE}/projects?per_page=100000", headers=auth(mint(tenant)))

    assert response.status_code == 200
    assert response.json["meta"]["per_page"] == 200


def test_a_bad_page_argument_is_a_400(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get(f"{BASE}/projects?page=banana", headers=auth(mint(tenant)))
    assert response.status_code == 400


def test_the_status_endpoint_reports_the_keys_own_scopes(
    client: FlaskClient, tenant: Tenant
) -> None:
    token = mint(tenant, scopes={PROJECT_READ, APPLICATION_READ})
    response = client.get(f"{BASE}/status", headers=auth(token))

    assert sorted(response.json["data"]["scopes"]) == sorted([PROJECT_READ, APPLICATION_READ])


def test_the_api_is_exempt_from_csrf(client: FlaskClient, tenant: Tenant) -> None:
    """A POST with a Bearer key and no CSRF token must not be rejected as CSRF.

    404 or 409 here is fine -- anything but the 400 that CSRF protection gives.
    """
    token = mint(tenant, scopes=ALL_PERMISSIONS)
    response = client.post(f"{BASE}/applications/nope/submit", headers=auth(token))

    assert response.status_code == 404


def test_a_refused_request_commits_nothing(client: FlaskClient, tenant: Tenant) -> None:
    """The error handlers render a message; they do not roll back.

    So the API's own after-request hook has to, or an endpoint that mutated and
    then raised would have its partial work committed on the way out. Probed
    through the one write that every request makes: authentication stamps
    ``last_used_at`` before the scope check runs, and a refused request must
    not keep it.
    """
    minted = apikeys.mint(tenant.organization, name="narrow", scopes={PROJECT_READ})
    db.session.commit()

    response = client.get(f"{BASE}/applications", headers=auth(minted.token))
    assert response.status_code == 403

    db.session.expire_all()
    assert db.session.get(ApiKey, minted.key.id).last_used_at is None
