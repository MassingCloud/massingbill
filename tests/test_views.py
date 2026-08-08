"""End-to-end journeys through the server-rendered UI.

These exercise the same path a user takes, so a broken template or a bad
``url_for`` fails here rather than in production.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from massingbill.extensions import db
from massingbill.models import CostCode, Project, Role, SovStatus
from massingbill.services import seeding
from massingbill.services import sov as sov_service
from tests.factories import Tenant, add_balanced_lines, make_tenant, sign_in


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return make_tenant("acme")


@pytest.fixture
def owner_client(client: FlaskClient, tenant: Tenant) -> FlaskClient:
    sign_in(client, tenant.user(Role.OWNER))
    return client


# ── Projects ────────────────────────────────────────────────────────────────


def test_creating_a_project_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        "/projects/new",
        data={
            "number": "2026-014",
            "name": "Harbour Point Phase 2",
            "address": "14 Dock Road",
            "jurisdiction_state": "TX",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Harbour Point Phase 2" in response.data

    created = db.session.scalar(select(Project).where(Project.number == "2026-014"))
    assert created is not None
    assert created.jurisdiction_state == "TX"


def test_a_duplicate_project_number_is_refused(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        "/projects/new",
        data={"number": tenant.project.number, "name": "Clash", "jurisdiction_state": "CA"},
    )
    assert response.status_code == 409


def test_a_project_form_without_a_state_is_rejected(owner_client: FlaskClient) -> None:
    """The state selects the retainage cap and waiver forms, so it is required."""
    response = owner_client.post("/projects/new", data={"number": "2026-015", "name": "No State"})
    assert response.status_code == 400


def test_editing_a_project(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        f"/projects/{tenant.project.id}/edit",
        data={
            "number": tenant.project.number,
            "name": "Renamed Project",
            "jurisdiction_state": "NY",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Renamed Project" in response.data
    assert tenant.project.jurisdiction_state == "NY"


def test_the_project_page_shows_the_contract_sum(owner_client: FlaskClient, tenant: Tenant) -> None:
    body = owner_client.get(f"/projects/{tenant.project.id}").get_data(as_text=True)
    assert "$12,450,000.00" in body


# ── Contract ────────────────────────────────────────────────────────────────


def test_saving_a_contract_parses_typed_money_and_percentages(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    """What a user types is what gets stored -- through the money kernel."""
    response = owner_client.post(
        f"/projects/{tenant.project.id}/contract",
        data={
            "number": "PC-002",
            "original_contract_sum": "$8,750,432.19",
            "retainage_rate_work": "5",
            "retainage_rate_stored": "2.5",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    contract = tenant.project.prime_contract
    assert contract is not None
    assert contract.original_contract_sum_cents == 875_043_219
    assert contract.retainage_rule is not None
    assert contract.retainage_rule.rate_work_bp == 500
    assert contract.retainage_rule.rate_stored_bp == 250


def test_a_sub_cent_contract_sum_is_rejected(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        f"/projects/{tenant.project.id}/contract",
        data={
            "original_contract_sum": "1000.005",
            "retainage_rate_work": "10",
            "retainage_rate_stored": "10",
        },
    )
    assert response.status_code == 400
    assert b"sub-cent" in response.data


def test_a_zero_contract_sum_is_rejected(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        f"/projects/{tenant.project.id}/contract",
        data={
            "original_contract_sum": "0",
            "retainage_rate_work": "10",
            "retainage_rate_stored": "10",
        },
    )
    assert response.status_code == 400
    assert b"greater than zero" in response.data


def test_a_rate_finer_than_a_basis_point_is_rejected(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    response = owner_client.post(
        f"/projects/{tenant.project.id}/contract",
        data={
            "original_contract_sum": "1000",
            "retainage_rate_work": "5.125",
            "retainage_rate_stored": "10",
        },
    )
    assert response.status_code == 400
    assert b"basis point" in response.data


# ── Schedule of values ──────────────────────────────────────────────────────


def test_the_schedule_page_shows_the_tie_out(owner_client: FlaskClient, tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=3)
    body = owner_client.get(f"/projects/{tenant.project.id}/sov/").get_data(as_text=True)

    assert "ties to the contract sum" in body
    assert "$12,450,000.00" in body


def test_an_unbalanced_schedule_is_flagged(owner_client: FlaskClient, tenant: Tenant) -> None:
    owner_client.post(
        f"/projects/{tenant.project.id}/sov/lines",
        data={"item_no": "001", "description": "Only line", "scheduled_value": "1,000.00"},
    )
    body = owner_client.get(f"/projects/{tenant.project.id}/sov/").get_data(as_text=True)
    assert "does not tie to the contract sum" in body


def test_adding_a_line_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    response = owner_client.post(
        f"/projects/{tenant.project.id}/sov/lines",
        data={
            "item_no": "003",
            "description": "Concrete",
            "csi_code": "03",
            "scheduled_value": "$1,250,000.00",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Concrete" in response.data

    line = tenant.schedule.lines[0]
    assert line.current_scheduled_value_cents == 125_000_000


def test_editing_a_line_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=2)
    line = tenant.schedule.lines[0]

    response = owner_client.post(
        f"/projects/{tenant.project.id}/sov/lines/{line.id}",
        data={"item_no": line.item_no, "description": "Revised scope", "scheduled_value": "500.00"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert line.description == "Revised scope"
    assert line.current_scheduled_value_cents == 50_000


def test_removing_a_line_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=3)
    line = tenant.schedule.lines[0]

    owner_client.post(
        f"/projects/{tenant.project.id}/sov/lines/{line.id}/remove", follow_redirects=True
    )
    assert len(tenant.schedule.lines) == 2


def test_approving_and_revising_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=4)

    approved = owner_client.post(
        f"/projects/{tenant.project.id}/sov/approve", follow_redirects=True
    )
    assert approved.status_code == 200
    assert tenant.schedule.status == SovStatus.APPROVED

    revised = owner_client.post(f"/projects/{tenant.project.id}/sov/revise", follow_redirects=True)
    assert revised.status_code == 200
    assert sov_service.current_schedule(tenant.contract).revision == 2


def test_approving_an_unbalanced_schedule_reports_the_gap(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    owner_client.post(
        f"/projects/{tenant.project.id}/sov/lines",
        data={"item_no": "001", "description": "Only line", "scheduled_value": "1,000.00"},
    )
    response = owner_client.post(f"/projects/{tenant.project.id}/sov/approve")

    assert response.status_code == 400
    assert b"does not tie to the contract sum" in response.data


def test_a_project_without_a_contract_has_no_schedule_page(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    bare = Project(
        organization_id=tenant.organization.id,
        number="2026-099",
        name="No contract yet",
        jurisdiction_state="CA",
    )
    db.session.add(bare)
    db.session.commit()

    response = owner_client.get(f"/projects/{bare.id}/sov/")
    assert response.status_code == 404
    assert b"no prime contract" in response.data


# ── Members and audit ───────────────────────────────────────────────────────


def test_the_member_list_renders(owner_client: FlaskClient, tenant: Tenant) -> None:
    body = owner_client.get("/organization/members").get_data(as_text=True)
    assert "owner@acme.example" in body
    assert "Project accountant" in body


def test_adding_a_member_who_has_no_account_explains_why_not(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    response = owner_client.post(
        "/organization/members",
        data={"email": "stranger@example.com", "role": "viewer"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"need to register first" in response.data


def test_adding_an_existing_user_as_a_member(owner_client: FlaskClient, tenant: Tenant) -> None:
    from tests.factories import make_user

    make_user("newhire@example.com")
    db.session.commit()

    response = owner_client.post(
        "/organization/members",
        data={"email": "newhire@example.com", "role": "pm"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"newhire@example.com" in response.data


def test_changing_a_member_role_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    from massingbill.services import accounts

    membership = next(
        m
        for m in accounts.memberships_for(tenant.user(Role.VIEWER))
        if m.organization_id == tenant.organization.id
    )
    owner_client.post(
        f"/organization/members/{membership.id}/role",
        data={"role": "accountant"},
        follow_redirects=True,
    )
    assert membership.role == Role.ACCOUNTANT


def test_removing_a_member_through_the_form(owner_client: FlaskClient, tenant: Tenant) -> None:
    from massingbill.services import accounts

    viewer = tenant.user(Role.VIEWER)
    membership = next(
        m for m in accounts.memberships_for(viewer) if m.organization_id == tenant.organization.id
    )
    owner_client.post(f"/organization/members/{membership.id}/remove", follow_redirects=True)
    assert accounts.memberships_for(viewer) == []


def test_removing_the_last_owner_is_refused_by_the_view(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    from massingbill.services import accounts

    membership = next(
        m
        for m in accounts.memberships_for(tenant.user(Role.OWNER))
        if m.organization_id == tenant.organization.id
    )
    response = owner_client.post(f"/organization/members/{membership.id}/remove")
    assert response.status_code == 409


def test_the_audit_page_reports_an_intact_chain(owner_client: FlaskClient, tenant: Tenant) -> None:
    body = owner_client.get("/organization/audit").get_data(as_text=True)
    assert "chain intact" in body
    assert "organization.created" in body


def test_the_two_factor_page_renders_an_enrolment_qr(
    owner_client: FlaskClient, tenant: Tenant
) -> None:
    body = owner_client.get("/auth/two-factor").get_data(as_text=True)
    assert "data:image/svg+xml;base64," in body


def test_switching_between_your_own_organizations(client: FlaskClient, app: Flask) -> None:
    from massingbill.services import accounts
    from tests.factories import make_user

    user = make_user("multi@example.com")
    first = accounts.create_organization("First Builders", user)
    second = accounts.create_organization("Second Builders", user)
    db.session.commit()

    sign_in(client, user)
    client.post(f"/auth/switch/{second.id}", follow_redirects=True)

    from massingbill.services.rbac import SESSION_ORG_KEY

    with client.session_transaction() as session:
        assert session[SESSION_ORG_KEY] == second.id
    assert first.id != second.id


# ── Seeding ─────────────────────────────────────────────────────────────────


def test_seeding_loads_the_csi_divisions(tenant: Tenant) -> None:
    added = seeding.seed_cost_codes(tenant.organization)
    db.session.commit()

    assert added > 30
    codes = list(
        db.session.scalars(
            select(CostCode.code).where(CostCode.organization_id == tenant.organization.id)
        )
    )
    assert "03" in codes  # Concrete
    assert "26" in codes  # Electrical


def test_seeding_twice_adds_nothing(tenant: Tenant) -> None:
    seeding.seed_cost_codes(tenant.organization)
    db.session.commit()

    assert seeding.seed_cost_codes(tenant.organization) == 0
