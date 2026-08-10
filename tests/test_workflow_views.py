"""Every workflow screen, loaded and posted to.

A Jinja typo is invisible until someone opens the page, and a `url_for` to a
route that does not exist raises only at render time. So every GET here is a
smoke test with teeth: it asserts a 200 and something from the page body, which
together mean the template resolved, every `url_for` in it resolved, and every
attribute it touches exists on the model.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.extensions import db
from massingbill.models import ComplianceKind, Role
from massingbill.services import change_order as co_service
from massingbill.services import sov as sov_service
from massingbill.services import subcontracts as sub_service
from massingbill.services import waivers as waiver_service
from tests.factories import Tenant, add_balanced_lines, make_tenant, sign_in


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    add_balanced_lines(built)
    sov_service.approve(built.schedule, actor=built.user(Role.OWNER))
    waiver_service.seed_templates(built.organization)
    db.session.commit()
    return built


def base(tenant: Tenant) -> str:
    return f"/projects/{tenant.project.id}"


# ── Every screen renders ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/change-orders", b"Change orders"),
        ("/waivers", b"Lien waivers"),
        ("/compliance", b"Compliance documents"),
        ("/subcontracts", b"Subcontracts"),
    ],
)
def test_the_screen_renders(client: FlaskClient, tenant: Tenant, path: str, marker: bytes) -> None:
    sign_in(client, tenant.user(Role.PM))
    response = client.get(f"{base(tenant)}{path}")

    assert response.status_code == 200, response.data[:400]
    assert marker in response.data


# ── Change orders ───────────────────────────────────────────────────────────


def test_creating_and_approving_a_change_order_moves_the_contract_sum(
    client: FlaskClient, tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.PM))

    client.post(
        f"{base(tenant)}/change-orders",
        data={"number": "CO-001", "description": "Rooftop screen"},
        follow_redirects=True,
    )
    order = co_service.for_contract(tenant.contract)[0]

    client.post(
        f"{base(tenant)}/change-orders/{order.id}/lines",
        data={"amount": "148000.00", "new_item_no": "013", "description": "Screen"},
        follow_redirects=True,
    )
    client.post(
        f"{base(tenant)}/change-orders/{order.id}/approve",
        data={"approved_date": "2026-04-14"},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert int(co_service.approved_total(tenant.contract)) == 148_000_00


def test_a_change_order_line_must_be_one_thing_or_the_other(
    client: FlaskClient, tenant: Tenant
) -> None:
    """Both would apply the amount twice; neither would apply it nowhere."""
    sign_in(client, tenant.user(Role.PM))
    client.post(
        f"{base(tenant)}/change-orders",
        data={"number": "CO-002", "description": "Ambiguous"},
        follow_redirects=True,
    )
    order = co_service.for_contract(tenant.contract)[0]

    response = client.post(
        f"{base(tenant)}/change-orders/{order.id}/lines",
        data={"amount": "1000.00"},  # neither an existing line nor a new one
        follow_redirects=True,
    )

    assert b"exactly one" in response.data
    assert order.lines == []


def test_approving_a_change_order_creates_a_schedule_revision(
    client: FlaskClient, tenant: Tenant
) -> None:
    """Editing the approved schedule in place would retroactively change what
    every issued application was built against."""
    sign_in(client, tenant.user(Role.PM))
    before = sov_service.approved_schedule(tenant.contract).revision

    client.post(
        f"{base(tenant)}/change-orders",
        data={"number": "CO-003", "description": "Extra"},
        follow_redirects=True,
    )
    order = co_service.for_contract(tenant.contract)[0]
    client.post(
        f"{base(tenant)}/change-orders/{order.id}/lines",
        data={"amount": "5000.00", "new_item_no": "099", "description": "Extra"},
        follow_redirects=True,
    )
    client.post(
        f"{base(tenant)}/change-orders/{order.id}/approve",
        data={"approved_date": "2026-04-14"},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert sov_service.approved_schedule(tenant.contract).revision == before + 1


# ── Waivers ─────────────────────────────────────────────────────────────────


def test_the_waiver_page_names_every_unverified_statutory_form(
    client: FlaskClient, tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.PM))
    response = client.get(f"{base(tenant)}/waivers")

    assert b"not yet verified" in response.data
    assert b"empty on purpose" in response.data


def test_verifying_a_template_makes_it_usable(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    template = waiver_service.unverified_templates(tenant.organization.id)[0]

    client.post(
        f"{base(tenant)}/waivers/templates/{template.id}/verify",
        data={"body": "X" * 80},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert db.session.get(type(template), template.id).is_usable


def test_a_viewer_cannot_verify_a_statutory_template(client: FlaskClient, tenant: Tenant) -> None:
    """Entering statutory text is the highest-consequence edit in the product."""
    sign_in(client, tenant.user(Role.VIEWER))
    template = waiver_service.unverified_templates(tenant.organization.id)[0]

    response = client.post(
        f"{base(tenant)}/waivers/templates/{template.id}/verify", data={"body": "X" * 80}
    )

    assert response.status_code == 403
    db.session.expire_all()
    assert not db.session.get(type(template), template.id).is_usable


# ── Compliance ──────────────────────────────────────────────────────────────


def test_adding_a_requirement_and_filing_against_it(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))

    client.post(
        f"{base(tenant)}/compliance",
        data={"kind": str(ComplianceKind.W9), "blocks_payment": "y"},
        follow_redirects=True,
    )

    from massingbill.services import compliance as compliance_service

    requirements = compliance_service.requirements_for(tenant.project)
    assert len(requirements) == 1

    response = client.post(
        f"{base(tenant)}/compliance/{requirements[0].id}/file",
        data={"kind": str(ComplianceKind.W9), "reference": "W9-2026.pdf"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    db.session.expire_all()
    assert compliance_service.requirements_for(tenant.project)[0].documents


# ── Subcontracts ────────────────────────────────────────────────────────────


def test_creating_a_subcontract_defaults_to_the_prime_retainage_rate(
    client: FlaskClient, tenant: Tenant
) -> None:
    """A sub held at a higher rate than the owner holds against the GC is how a
    contractor finances someone else's job by accident."""
    sign_in(client, tenant.user(Role.PM))

    client.post(
        f"{base(tenant)}/subcontracts",
        data={
            "number": "SC-001",
            "vendor_name": "Cascade Glazing",
            "amount": "245000.00",
            "retainage_bp": "",
        },
        follow_redirects=True,
    )

    subs = sub_service.for_project(tenant.project)
    assert len(subs) == 1
    assert subs[0].retainage_rate_bp == tenant.contract.retainage_rule.rate_work_bp


def test_an_explicit_subcontract_retainage_rate_is_honoured(
    client: FlaskClient, tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.PM))
    client.post(
        f"{base(tenant)}/subcontracts",
        data={
            "number": "SC-002",
            "vendor_name": "Ridge Steel",
            "amount": "100000.00",
            "retainage_bp": "5",
        },
        follow_redirects=True,
    )

    assert sub_service.for_project(tenant.project)[0].retainage_rate_bp == 500


# ── Authorization ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/change-orders", "/waivers", "/compliance", "/subcontracts"])
def test_a_signed_out_visitor_is_sent_to_sign_in(
    client: FlaskClient, tenant: Tenant, path: str
) -> None:
    response = client.get(f"{base(tenant)}{path}")
    assert response.status_code in (302, 401)


@pytest.mark.parametrize("path", ["/change-orders", "/waivers", "/compliance", "/subcontracts"])
def test_no_workflow_screen_leaks_across_tenants(
    client: FlaskClient, tenant: Tenant, app: Flask, path: str
) -> None:
    stranger = make_tenant("rival")
    db.session.commit()

    sign_in(client, tenant.user(Role.PM))
    response = client.get(f"/projects/{stranger.project.id}{path}")

    assert response.status_code == 404
