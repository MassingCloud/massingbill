"""Authorization: the role x route matrix and cross-tenant probes.

These are the P2 acceptance criteria. A billing system that shows one
contractor another contractor's schedule of values does not get a second
chance, so the cross-tenant probes cover **every** resource type rather than a
representative sample.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.models import Role
from massingbill.services.rbac import (
    ALL_PERMISSIONS,
    ROLE_PERMISSIONS,
    permissions_for,
)
from tests.factories import Tenant, add_balanced_lines, make_tenant, sign_in


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


# ── The permission table itself ─────────────────────────────────────────────


def test_every_role_has_an_explicit_entry() -> None:
    """A role with no entry would silently get no permissions -- or, worse,
    someone would 'fix' that by defaulting to a permissive fallback."""
    for role in Role:
        assert role in ROLE_PERMISSIONS, f"{role} has no permission set"


def test_no_role_grants_an_unknown_permission() -> None:
    for role, granted in ROLE_PERMISSIONS.items():
        unknown = granted - ALL_PERMISSIONS
        assert not unknown, f"{role} grants unknown permission(s): {unknown}"


def test_the_owner_can_do_everything() -> None:
    assert permissions_for(Role.OWNER) == ALL_PERMISSIONS


def test_an_accountant_writes_the_schedule_but_does_not_approve_it() -> None:
    """Separation of duties: whoever writes the numbers should not be the only
    person who blesses them."""
    granted = permissions_for(Role.ACCOUNTANT)
    assert "sov:write" in granted
    assert "sov:approve" not in granted


def test_a_viewer_cannot_write_anything() -> None:
    granted = permissions_for(Role.VIEWER)
    assert all(not p.endswith((":write", ":create", ":update", ":delete")) for p in granted)


def test_counterparties_cannot_manage_the_organization() -> None:
    for role in (Role.EXTERNAL_APPROVER, Role.SUB_CONTACT):
        assert "org:manage" not in permissions_for(role)
        assert "audit:read" not in permissions_for(role)


# ── The role x route matrix ─────────────────────────────────────────────────

#: (method, url template, the permission the route requires)
ROUTES = [
    ("GET", "/projects", "project:read"),
    ("GET", "/projects/new", "project:create"),
    ("GET", "/projects/{project_id}", "project:read"),
    ("GET", "/projects/{project_id}/edit", "project:update"),
    ("GET", "/projects/{project_id}/contract", "project:update"),
    ("GET", "/projects/{project_id}/sov/", "sov:read"),
    ("GET", "/organization/members", "org:manage"),
    ("GET", "/organization/audit", "audit:read"),
]


@pytest.mark.parametrize(("method", "template", "permission"), ROUTES)
@pytest.mark.parametrize("role", list(Role))
def test_every_role_against_every_route(
    client: FlaskClient,
    tenant: Tenant,
    role: Role,
    method: str,
    template: str,
    permission: str,
) -> None:
    """The whole matrix: 7 roles x 8 routes, allowed exactly when the
    permission table says so."""
    sign_in(client, tenant.user(role))
    url = template.format(project_id=tenant.project.id)

    response = client.open(url, method=method)
    allowed = permission in permissions_for(role)

    if allowed:
        assert response.status_code == 200, f"{role} should reach {url}"
    else:
        assert response.status_code == 403, f"{role} should be refused {url}"


def test_signed_out_users_are_sent_to_sign_in(client: FlaskClient, tenant: Tenant) -> None:
    response = client.get("/projects")
    assert response.status_code == 302
    assert "/auth/sign-in" in response.headers["Location"]


def test_a_write_route_refuses_a_role_without_the_permission(
    client: FlaskClient, tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.VIEWER))
    response = client.post(
        "/projects/new", data={"number": "X", "name": "Y", "jurisdiction_state": "CA"}
    )
    assert response.status_code == 403


def test_an_accountant_cannot_approve_a_schedule(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.ACCOUNTANT))
    response = client.post(f"/projects/{tenant.project.id}/sov/approve")
    assert response.status_code == 403


# ── Cross-tenant isolation ──────────────────────────────────────────────────


def test_a_project_from_another_tenant_is_not_found(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """404, not 403. Confirming the id exists tells one contractor that another
    contractor's project is real."""
    sign_in(client, tenant.user(Role.OWNER))
    response = client.get(f"/projects/{other_tenant.project.id}")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "template",
    [
        "/projects/{project_id}",
        "/projects/{project_id}/edit",
        "/projects/{project_id}/contract",
        "/projects/{project_id}/sov/",
    ],
)
def test_no_project_route_leaks_across_tenants(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant, template: str
) -> None:
    sign_in(client, tenant.user(Role.OWNER))
    response = client.get(template.format(project_id=other_tenant.project.id))
    assert response.status_code == 404


def test_a_schedule_line_from_another_tenant_is_not_reachable(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    foreign_line = other_tenant.schedule.lines[0]
    sign_in(client, tenant.user(Role.OWNER))

    response = client.get(f"/projects/{tenant.project.id}/sov/lines/{foreign_line.id}")
    assert response.status_code == 404


def test_a_membership_from_another_tenant_cannot_be_edited(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    from sqlalchemy import select

    from massingbill.extensions import db
    from massingbill.models import Membership

    foreign = db.session.scalar(
        select(Membership).where(Membership.organization_id == other_tenant.organization.id)
    )
    assert foreign is not None

    sign_in(client, tenant.user(Role.OWNER))
    response = client.post(f"/organization/members/{foreign.id}/role", data={"role": "viewer"})
    assert response.status_code == 404


def test_the_project_list_shows_only_your_own(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.OWNER))
    body = client.get("/projects").get_data(as_text=True)

    assert tenant.project.name in body
    assert other_tenant.project.name not in body


def test_switching_to_an_organization_you_do_not_belong_to_is_refused(
    client: FlaskClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.OWNER))
    client.post(f"/auth/switch/{other_tenant.organization.id}", follow_redirects=True)

    # Still scoped to the original tenant.
    body = client.get("/projects").get_data(as_text=True)
    assert other_tenant.project.name not in body


def test_losing_your_membership_takes_effect_on_the_next_request(
    client: FlaskClient, tenant: Tenant
) -> None:
    """Permissions are re-read per request, not cached at sign-in."""
    from massingbill.extensions import db
    from massingbill.services import accounts

    viewer = tenant.user(Role.VIEWER)
    sign_in(client, viewer)
    assert client.get("/projects").status_code == 200

    membership = next(
        m for m in accounts.memberships_for(viewer) if m.organization_id == tenant.organization.id
    )
    accounts.remove_member(membership, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert client.get("/projects").status_code == 401
