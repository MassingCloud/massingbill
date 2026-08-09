"""Subcontracts, sub billings, and recorded payments.

The AP mirror of the prime side, plus the payment record that makes
``PAY-VARIANCE`` and conditional-waiver effectiveness provable.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.errors import ConflictError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    ApplicationStatus,
    PaymentMethod,
    Role,
    SubApplicationStatus,
    SubcontractStatus,
)
from massingbill.services import application as app_service
from massingbill.services import payments as payment_service
from massingbill.services import sov as sov_service
from massingbill.services import subcontracts as sub_service
from massingbill.services import tieout
from massingbill.services.money import cents
from tests.factories import Tenant, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("subs", contract_sum_cents=1_000_000_00)
    for item, value in (("001", 600_000_00), ("002", 400_000_00)):
        sov_service.add_line(
            built.schedule,
            sov_service.LineInput(
                item_no=item, description=f"Line {item}", scheduled_value_cents=cents(value)
            ),
            actor=built.user(Role.OWNER),
        )
    sov_service.approve(built.schedule, actor=built.user(Role.OWNER))
    db.session.commit()
    return built


@pytest.fixture
def application(tenant: Tenant):
    built = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        actor=tenant.user(Role.OWNER),
    )
    app_service.enter(
        built,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(built.lines, [300_000_00, 100_000_00], strict=True)
        ],
    )
    return built


@pytest.fixture
def subcontract(tenant: Tenant):
    built = sub_service.create(
        tenant.project,
        number="SC-001",
        vendor_name="Delta Mechanical",
        amount=cents(250_000_00),
        scope="HVAC",
        csi_code="23",
        contact_email="ap@delta.example",
        actor=tenant.user(Role.OWNER),
    )
    sub_service.execute(built, on=date(2026, 1, 15))
    db.session.commit()
    return built


# ── Subcontracts ────────────────────────────────────────────────────────────


def test_creating_a_subcontract(tenant: Tenant, subcontract) -> None:
    assert subcontract.status == SubcontractStatus.EXECUTED
    assert subcontract.current_amount_cents == 250_000_00
    assert subcontract.contact_email == "ap@delta.example"


def test_duplicate_numbers_are_refused(tenant: Tenant, subcontract) -> None:
    with pytest.raises(ConflictError, match="already exists"):
        sub_service.create(
            tenant.project, number="SC-001", vendor_name="Someone Else", amount=cents(1_000_00)
        )


def test_a_zero_amount_subcontract_is_refused(tenant: Tenant) -> None:
    with pytest.raises(ValidationError, match="greater than zero"):
        sub_service.create(tenant.project, number="SC-002", vendor_name="Nobody", amount=cents(0))


def test_the_current_amount_is_derived_not_overwritten(tenant: Tenant, subcontract) -> None:
    """Same rule as the prime side: a change order adjusts, it does not restate."""
    subcontract.co_adjustment_cents = 15_000_00
    db.session.flush()

    assert subcontract.original_amount_cents == 250_000_00
    assert subcontract.current_amount_cents == 265_000_00


def test_lines_roll_up_to_a_prime_schedule_line(tenant: Tenant, subcontract) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    line = sub_service.add_line(
        subcontract,
        item_no="A",
        description="Ductwork",
        amount=cents(150_000_00),
        sov_line_id=schedule.lines[0].id,
    )
    assert line.sov_line_id == schedule.lines[0].id


def test_committed_total_sums_the_subcontracts(tenant: Tenant, subcontract) -> None:
    sub_service.create(
        tenant.project, number="SC-002", vendor_name="Acme Electric", amount=cents(180_000_00)
    )
    db.session.flush()

    assert sub_service.committed_total(tenant.project) == 430_000_00


# ── Billings ────────────────────────────────────────────────────────────────


def test_a_billing_withholds_the_subcontract_retainage(tenant: Tenant, subcontract) -> None:
    """What the subcontract says is withheld is what gets withheld, whatever
    arrived on the sub's paperwork."""
    billing = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )

    assert billing.retainage_cents == 10_000_00  # 10%
    assert billing.previous_payments_cents == 0
    assert billing.payment_due_cents == 90_000_00
    assert billing.status == SubApplicationStatus.RECEIVED


def test_a_second_billing_deducts_what_was_approved_before(tenant: Tenant, subcontract) -> None:
    first = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )
    sub_service.approve(first)

    second = sub_service.receive(
        subcontract,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        completed_to_date=cents(180_000_00),
    )

    assert second.previous_payments_cents == 90_000_00
    assert second.retainage_cents == 18_000_00
    assert second.payment_due_cents == 180_000_00 - 18_000_00 - 90_000_00


def test_a_partly_approved_billing_carries_forward_the_approved_amount(
    tenant: Tenant, subcontract
) -> None:
    """The same rule as the prime side's line 7: what the GC declined to approve
    has not been paid, and must not be treated as if it had."""
    first = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )
    sub_service.approve(first, amount=cents(70_000_00), note="Duct hangers not accepted")

    second = sub_service.receive(
        subcontract,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        completed_to_date=cents(180_000_00),
    )
    assert second.previous_payments_cents == 70_000_00


def test_a_final_billing_releases_retainage(tenant: Tenant, subcontract) -> None:
    billing = sub_service.receive(
        subcontract,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        completed_to_date=cents(250_000_00),
        is_final=True,
    )
    assert billing.retainage_cents == 0
    assert billing.payment_due_cents == 250_000_00


def test_a_billing_beyond_the_subcontract_value_is_refused(tenant: Tenant, subcontract) -> None:
    with pytest.raises(ValidationError, match="against a value of"):
        sub_service.receive(
            subcontract,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            completed_to_date=cents(300_000_00),
        )


def test_a_billing_cannot_be_approved_for_more_than_it_claims(tenant: Tenant, subcontract) -> None:
    billing = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )
    with pytest.raises(ValidationError, match="cannot be approved for more"):
        sub_service.approve(billing, amount=cents(999_999_00))


def test_a_rejected_billing_cannot_be_approved(tenant: Tenant, subcontract) -> None:
    billing = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )
    sub_service.reject(billing, reason="No certified payroll")

    assert billing.status == SubApplicationStatus.REJECTED
    with pytest.raises(ConflictError, match="cannot be approved"):
        sub_service.approve(billing)


def test_a_backwards_period_is_refused(tenant: Tenant, subcontract) -> None:
    with pytest.raises(ValidationError, match="cannot precede"):
        sub_service.receive(
            subcontract,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 7, 1),
            completed_to_date=cents(1_000_00),
        )


def test_approved_to_date_counts_only_approved_billings(tenant: Tenant, subcontract) -> None:
    first = sub_service.receive(
        subcontract,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        completed_to_date=cents(100_000_00),
    )
    sub_service.approve(first)

    second = sub_service.receive(
        subcontract,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        completed_to_date=cents(150_000_00),
    )
    assert second is not None

    assert sub_service.approved_to_date(subcontract) == 90_000_00


# ── Payments ────────────────────────────────────────────────────────────────


def _submitted(tenant: Tenant, application):
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()
    return application


def test_a_payment_cannot_be_recorded_against_a_draft(tenant: Tenant, application) -> None:
    with pytest.raises(ValidationError, match="has not been submitted"):
        payment_service.record(application, amount=cents(1_000_00), received_on=date(2026, 8, 15))


def test_recording_a_full_payment_closes_the_period(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    due = application.certified_payment_cents

    payment_service.record(
        application,
        amount=cents(due),
        received_on=date(2026, 8, 15),
        method=PaymentMethod.ACH,
        reference="ACH-88213",
    )

    assert application.status == ApplicationStatus.PAID
    assert payment_service.paid_to_date(application) == due
    assert payment_service.variance(application) == 0


def test_a_part_payment_leaves_the_period_open(tenant: Tenant, application) -> None:
    """A conditional waiver has not taken effect on a part payment, so the
    period must not be marked paid."""
    _submitted(tenant, application)

    payment_service.record(application, amount=cents(50_000_00), received_on=date(2026, 8, 15))

    assert application.status != ApplicationStatus.PAID
    assert payment_service.variance(application) > 0


def test_two_part_payments_add_up_and_close_the_period(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    due = application.certified_payment_cents

    payment_service.record(application, amount=cents(50_000_00), received_on=date(2026, 8, 10))
    payment_service.record(
        application, amount=cents(due - 50_000_00), received_on=date(2026, 8, 20)
    )

    assert payment_service.paid_to_date(application) == due
    assert application.status == ApplicationStatus.PAID


def test_a_zero_payment_is_refused(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    with pytest.raises(ValidationError, match="needs an amount"):
        payment_service.record(application, amount=cents(0), received_on=date(2026, 8, 15))


def test_a_joint_cheque_records_the_second_payee(tenant: Tenant, application) -> None:
    """We record the arrangement because the release depends on it. We do not
    issue the cheque."""
    _submitted(tenant, application)

    payment = payment_service.record(
        application,
        amount=cents(10_000_00),
        received_on=date(2026, 8, 15),
        method=PaymentMethod.JOINT_CHECK,
        joint_payee="Kawneer Supply Co.",
    )
    assert payment.joint_payee == "Kawneer Supply Co."


def test_payments_are_listed_in_date_order(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    payment_service.record(application, amount=cents(2_000_00), received_on=date(2026, 9, 1))
    payment_service.record(application, amount=cents(1_000_00), received_on=date(2026, 8, 1))

    dates = [p.received_on for p in payment_service.payments_for(application)]
    assert dates == sorted(dates)


# ── PAY-VARIANCE (competitive-upgrades.md U3) ───────────────────────────────


def _ids(application) -> set[str]:
    return {f.rule_id for f in tieout.run(application).findings}


def test_no_payment_recorded_means_no_variance_finding(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    assert "PAY-VARIANCE" not in _ids(application)


def test_a_short_payment_is_reported(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    payment_service.record(application, amount=cents(50_000_00), received_on=date(2026, 8, 15))

    report = tieout.run(application)
    finding = next(f for f in report.findings if f.rule_id == "PAY-VARIANCE")

    assert "still outstanding" in finding.message
    assert report.ok, "a short payment is a fact about the owner, not a defect"


def test_a_payment_matching_the_certificate_reports_nothing(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    payment_service.record(
        application,
        amount=cents(application.certified_payment_cents),
        received_on=date(2026, 8, 15),
    )
    assert "PAY-VARIANCE" not in _ids(application)


def test_an_overpayment_is_reported(tenant: Tenant, application) -> None:
    _submitted(tenant, application)
    payment_service.record(
        application,
        amount=cents(application.certified_payment_cents + 5_000_00),
        received_on=date(2026, 8, 15),
    )

    finding = next(f for f in tieout.run(application).findings if f.rule_id == "PAY-VARIANCE")
    assert "more than certified" in finding.message


def test_variance_follows_the_certificate_not_the_request(tenant: Tenant, application) -> None:
    """An architect who certifies less changes what is owed, so the variance is
    measured against the certificate."""
    _submitted(tenant, application)
    requested = application.line8_current_payment_due

    app_service.certify(application, cents(requested - 20_000_00), certified_by_label="Architect")
    payment_service.record(
        application, amount=cents(requested - 20_000_00), received_on=date(2026, 8, 15)
    )

    assert payment_service.variance(application) == 0
    assert "PAY-VARIANCE" not in _ids(application)


# ── Party details (competitive-upgrades.md U2) ──────────────────────────────


def test_a_project_without_an_address_is_flagged(tenant: Tenant, application) -> None:
    tenant.project.address = ""
    db.session.flush()

    assert "ADDRESS-MISSING" in _ids(application)


def test_a_project_without_a_state_is_flagged(tenant: Tenant, application) -> None:
    tenant.project.jurisdiction_state = ""
    db.session.flush()

    assert "PARTY-MISSING" in _ids(application)


def test_a_complete_project_is_not_flagged(tenant: Tenant, application) -> None:
    assert "ADDRESS-MISSING" not in _ids(application)
    assert "PARTY-MISSING" not in _ids(application)
