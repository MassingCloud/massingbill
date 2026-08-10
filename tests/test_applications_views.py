"""The requisition worksheet, driven the way a project accountant drives it.

These go through HTTP rather than calling the service directly. The service
layer is already covered; what is unproven until you post a form is that the
screen collects the right values, refuses the right things, and shows the
reconciliation before anyone can submit past it.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.extensions import db
from massingbill.models import ApplicationStatus, Role
from massingbill.services import application as app_service
from massingbill.services import sov as sov_service
from tests.factories import Tenant, add_balanced_lines, make_tenant, sign_in


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    add_balanced_lines(built)
    sov_service.approve(built.schedule, actor=built.user(Role.OWNER))
    db.session.commit()
    return built


def base(tenant: Tenant) -> str:
    return f"/projects/{tenant.project.id}/applications"


def open_period(client: FlaskClient, tenant: Tenant) -> object:
    client.post(
        f"{base(tenant)}/open",
        data={"period_start": "2026-02-01", "period_end": "2026-02-28"},
        follow_redirects=True,
    )
    return app_service.open_application(tenant.contract)


# ── The list ────────────────────────────────────────────────────────────────


def test_the_index_renders(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    # Trailing slash: the blueprint publishes "/", so the bare path is a 308.
    response = client.get(f"{base(tenant)}/")

    assert response.status_code == 200
    assert b"Applications for payment" in response.data


def test_a_viewer_cannot_open_a_period(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.VIEWER))
    response = client.post(
        f"{base(tenant)}/open", data={"period_start": "2026-02-01", "period_end": "2026-02-28"}
    )

    assert response.status_code == 403
    assert app_service.open_application(tenant.contract) is None


def test_opening_a_period_creates_one(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)

    assert application is not None
    assert application.number == 1
    assert application.status == ApplicationStatus.DRAFT


def test_a_period_that_ends_before_it_starts_is_refused(
    client: FlaskClient, tenant: Tenant
) -> None:
    sign_in(client, tenant.user(Role.PM))
    response = client.post(
        f"{base(tenant)}/open",
        data={"period_start": "2026-02-28", "period_end": "2026-02-01"},
        follow_redirects=True,
    )

    assert b"cannot end before it starts" in response.data
    assert app_service.open_application(tenant.contract) is None


# ── The worksheet ───────────────────────────────────────────────────────────


def test_the_worksheet_shows_the_reconciliation(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)

    response = client.get(f"{base(tenant)}/{application.id}")

    assert response.status_code == 200
    assert b"Reconciliation" in response.data
    assert b"Continuation sheet" in response.data


def test_entering_work_updates_every_derived_figure(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]

    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
        follow_redirects=True,
    )

    db.session.expire_all()
    application = app_service.open_application(tenant.contract)
    assert application.lines[0].col_e_this_period == 1000_00
    assert application.line4_completed_stored == 1000_00


def test_a_bad_amount_is_reported_against_its_own_line(client: FlaskClient, tenant: Tenant) -> None:
    """Sub-cent precision is refused rather than rounded, and the message has to
    say which line it came from -- a sheet of forty rows with one unattributed
    error is unusable."""
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]

    response = client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.005"},
        follow_redirects=True,
    )

    assert f"Line {first.item_no}".encode() in response.data


def test_one_bad_line_saves_none_of_them(client: FlaskClient, tenant: Tenant) -> None:
    """A partial save leaves a period that looks entered and is not."""
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    good, bad = application.lines[0], application.lines[1]

    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={
            f"line-{good.id}-this_period": "2500.00",
            f"line-{bad.id}-this_period": "not a number",
        },
        follow_redirects=True,
    )

    db.session.expire_all()
    application = app_service.open_application(tenant.contract)
    assert application.lines[0].col_e_this_period == 0, "the good line was saved anyway"


def test_an_accountant_can_enter_but_not_submit(client: FlaskClient, tenant: Tenant) -> None:
    """Separation of duties, enforced at the route rather than only in the table."""
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    client.get("/auth/sign-out")

    sign_in(client, tenant.user(Role.ACCOUNTANT))
    first = application.lines[0]

    entered = client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
    )
    assert entered.status_code in (302, 303)

    refused = client.post(f"{base(tenant)}/{application.id}/submit")
    assert refused.status_code == 403


# ── Submission ──────────────────────────────────────────────────────────────


def test_submitting_freezes_the_period(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]

    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
        follow_redirects=True,
    )
    response = client.post(f"{base(tenant)}/{application.id}/submit", follow_redirects=True)

    db.session.expire_all()
    frozen = db.session.get(type(application), application.id)
    assert frozen.status == ApplicationStatus.SUBMITTED
    assert b"submitted" in response.data.lower()


def test_a_frozen_application_refuses_further_entry(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]

    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
        follow_redirects=True,
    )
    client.post(f"{base(tenant)}/{application.id}/submit", follow_redirects=True)

    response = client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "9999.00"},
    )
    assert response.status_code == 400


# ── Certification ───────────────────────────────────────────────────────────


def test_certifying_less_requires_a_reason(client: FlaskClient, tenant: Tenant) -> None:
    """The difference between requested and certified is the thing every party
    asks about later. Recording it without a reason makes the record useless."""
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]
    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
        follow_redirects=True,
    )
    client.post(f"{base(tenant)}/{application.id}/submit", follow_redirects=True)
    client.get("/auth/sign-out")

    sign_in(client, tenant.user(Role.ACCOUNTANT))
    response = client.post(
        f"{base(tenant)}/{application.id}/certify",
        data={
            "amount_certified": "500.00",
            "certified_by_label": "Ferris & Partners",
            "reason": "",
        },
        follow_redirects=True,
    )

    assert b"Record why" in response.data
    db.session.expire_all()
    assert db.session.get(type(application), application.id).certification is None


def test_certifying_in_full_needs_no_reason(client: FlaskClient, tenant: Tenant) -> None:
    sign_in(client, tenant.user(Role.PM))
    application = open_period(client, tenant)
    first = application.lines[0]
    client.post(
        f"{base(tenant)}/{application.id}/enter",
        data={f"line-{first.id}-this_period": "1000.00"},
        follow_redirects=True,
    )
    client.post(f"{base(tenant)}/{application.id}/submit", follow_redirects=True)

    db.session.expire_all()
    due = db.session.get(type(application), application.id).line8_current_payment_due

    client.get("/auth/sign-out")
    sign_in(client, tenant.user(Role.ACCOUNTANT))
    client.post(
        f"{base(tenant)}/{application.id}/certify",
        data={
            "amount_certified": str(due / 100),
            "certified_by_label": "Ferris & Partners",
        },
        follow_redirects=True,
    )

    db.session.expire_all()
    certified = db.session.get(type(application), application.id)
    assert certified.status == ApplicationStatus.CERTIFIED
    assert certified.certification.variance_cents == 0


# ── Tenant isolation ────────────────────────────────────────────────────────


def test_an_application_from_another_project_is_not_found(
    client: FlaskClient, tenant: Tenant, app: Flask
) -> None:
    """A real id under the wrong project would otherwise render a quietly wrong
    page rather than an error."""
    other = make_tenant("rival")
    add_balanced_lines(other)
    sov_service.approve(other.schedule, actor=other.user(Role.OWNER))
    db.session.commit()

    sign_in(client, other.user(Role.PM))
    stranger = app_service.open_period(
        other.contract,
        period_start=__import__("datetime").date(2026, 2, 1),
        period_end=__import__("datetime").date(2026, 2, 28),
        actor=other.user(Role.PM),
    )
    db.session.commit()

    client.get("/auth/sign-out")
    sign_in(client, tenant.user(Role.PM))

    response = client.get(f"{base(tenant)}/{stranger.id}")
    assert response.status_code == 404
