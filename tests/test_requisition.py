"""The requisition engine: periods, retainage, change orders, stored materials.

Worked arithmetic, checkable by hand. The multi-period golden project lives in
``test_golden_project.py``.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.errors import ConflictError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    ApplicationStatus,
    ChangeOrderStatus,
    RetainageMode,
    Role,
    StorageLocation,
    StoredMaterial,
)
from massingbill.services import application as app_service
from massingbill.services import change_order as co_service
from massingbill.services import retainage, tieout
from massingbill.services import sov as sov_service
from massingbill.services.money import bp, cents
from tests.factories import Tenant, make_tenant


def _billable(slug: str = "acme", contract_sum: int = 1_000_000_00) -> Tenant:
    """A tenant with an approved three-line schedule ready to bill against.

    $1,000,000.00 across three lines of $500k / $300k / $200k -- round numbers
    so every expectation below can be checked in your head.
    """
    tenant = make_tenant(slug, contract_sum_cents=contract_sum)
    for item, value in (("001", 500_000_00), ("002", 300_000_00), ("003", 200_000_00)):
        sov_service.add_line(
            tenant.schedule,
            sov_service.LineInput(
                item_no=item, description=f"Division {item}", scheduled_value_cents=cents(value)
            ),
            actor=tenant.user(Role.OWNER),
        )
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()
    return tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return _billable()


def _open(tenant: Tenant, month: int = 1):
    return app_service.open_period(
        tenant.contract,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        actor=tenant.user(Role.OWNER),
    )


def _enter(application, values: list[tuple[int, int]]) -> None:
    """values: (this_period, stored) per line, in order."""
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(work), stored=cents(stored))
            for line, (work, stored) in zip(application.lines, values, strict=True)
        ],
    )


# ── Opening a period ────────────────────────────────────────────────────────


def test_a_period_cannot_open_before_the_schedule_is_approved(app: Flask) -> None:
    unapproved = make_tenant("draft")
    with pytest.raises(ConflictError, match="must be approved"):
        app_service.open_period(
            unapproved.contract, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
        )


def test_the_first_period_starts_at_zero(tenant: Tenant) -> None:
    application = _open(tenant)

    assert application.number == 1
    assert application.line1_original_sum == 1_000_000_00
    assert application.line2_net_co == 0
    assert application.line3_contract_sum_to_date == 1_000_000_00
    assert application.line4_completed_stored == 0
    assert application.line7_previous_certificates == 0
    assert application.line9_balance_to_finish == 1_000_000_00
    assert all(line.col_d_previous == 0 for line in application.lines)


def test_only_one_period_may_be_open(tenant: Tenant) -> None:
    _open(tenant, month=1)
    with pytest.raises(ConflictError, match="still open"):
        _open(tenant, month=2)


def test_periods_may_not_overlap(tenant: Tenant) -> None:
    first = _open(tenant, month=1)
    _enter(first, [(0, 0), (0, 0), (0, 0)])
    app_service.submit(first, actor=tenant.user(Role.OWNER))

    with pytest.raises(ValidationError, match="overlaps"):
        app_service.open_period(
            tenant.contract, period_start=date(2026, 1, 15), period_end=date(2026, 2, 15)
        )


def test_a_backwards_period_is_refused(tenant: Tenant) -> None:
    with pytest.raises(ValidationError, match="cannot precede"):
        app_service.open_period(
            tenant.contract, period_start=date(2026, 3, 1), period_end=date(2026, 2, 1)
        )


# ── The G702 arithmetic ─────────────────────────────────────────────────────


def test_a_first_application_with_ten_percent_retainage(tenant: Tenant) -> None:
    """$150,000 billed against a $1,000,000 contract at 10%.

    Line 4 = 150,000. Line 5a = 15,000. Line 6 = 135,000. Line 7 = 0.
    Line 8 = 135,000. Line 9 = 1,000,000 - 135,000 = 865,000.
    """
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (50_000_00, 0), (0, 0)])

    assert application.line4_completed_stored == 150_000_00
    assert application.line5a_retainage_work == 15_000_00
    assert application.line5b_retainage_stored == 0
    assert application.line5_total_retainage == 15_000_00
    assert application.line6_earned_less_retainage == 135_000_00
    assert application.line7_previous_certificates == 0
    assert application.line8_current_payment_due == 135_000_00
    assert application.line9_balance_to_finish == 865_000_00


def test_the_g703_columns_derive_correctly(tenant: Tenant) -> None:
    application = _open(tenant)
    _enter(application, [(100_000_00, 25_000_00), (0, 0), (0, 0)])

    line = application.lines[0]
    assert line.col_c_scheduled_value == 500_000_00
    assert line.col_d_previous == 0
    assert line.col_e_this_period == 100_000_00
    assert line.col_f_stored == 25_000_00
    assert line.col_g_completed_stored == 125_000_00
    assert line.col_h_balance == 375_000_00
    assert line.percent_complete_bp == 2500  # 25.00%


def test_split_retainage_uses_separate_rates(tenant: Tenant) -> None:
    """G702 line 5a and 5b: 10% on work, 5% on stored material."""
    tenant.contract.retainage_rule.rate_stored_bp = 500
    db.session.flush()

    application = _open(tenant)
    _enter(application, [(100_000_00, 40_000_00), (0, 0), (0, 0)])

    assert application.line5a_retainage_work == 10_000_00  # 10% of 100,000
    assert application.line5b_retainage_stored == 2_000_00  # 5% of 40,000
    assert application.line5_total_retainage == 12_000_00


def test_the_second_period_carries_column_d_forward(tenant: Tenant) -> None:
    first = _open(tenant, month=1)
    _enter(first, [(100_000_00, 0), (50_000_00, 0), (0, 0)])
    app_service.submit(first, actor=tenant.user(Role.OWNER))
    db.session.commit()

    second = _open(tenant, month=2)

    assert [line.col_d_previous for line in second.lines] == [100_000_00, 50_000_00, 0]
    assert second.line7_previous_certificates == 135_000_00  # the first period's line 6


def test_a_voided_period_does_not_affect_the_carry_forward(tenant: Tenant) -> None:
    """An abandoned draft never billed anything, so column D and line 7 must
    not see it -- but it keeps its number, because a void is a record that an
    application existed and was withdrawn."""
    abandoned = _open(tenant, month=1)
    _enter(abandoned, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.void(abandoned, reason="entered against the wrong project")
    db.session.commit()

    fresh = _open(tenant, month=1)

    assert fresh.number == 2, "a voided application keeps its number"
    assert all(line.col_d_previous == 0 for line in fresh.lines)
    assert fresh.line7_previous_certificates == 0


def test_line_seven_follows_the_certificate_not_the_request(tenant: Tenant) -> None:
    """The subtle one. G702 line 7 is 'less previous *certificates*'. When an
    architect certifies less than was requested, the next period must pick up
    the certified figure or the contractor bills the shortfall twice."""
    first = _open(tenant, month=1)
    _enter(first, [(100_000_00, 0), (50_000_00, 0), (0, 0)])
    app_service.submit(first, actor=tenant.user(Role.OWNER))

    assert first.line8_current_payment_due == 135_000_00

    app_service.certify(
        first,
        cents(125_000_00),
        certified_by_label="Ferris & Partners, Architects",
        reason="Line 002 not accepted as complete",
    )
    db.session.commit()

    second = _open(tenant, month=2)
    assert second.line7_previous_certificates == 125_000_00, (
        "line 7 must follow the certificate, not the request"
    )


def test_certification_records_the_variance(tenant: Tenant) -> None:
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.submit(application, actor=tenant.user(Role.OWNER))

    certification = app_service.certify(
        application, cents(80_000_00), certified_by_label="Architect"
    )
    assert certification.variance_cents == 80_000_00 - 90_000_00
    assert application.status == ApplicationStatus.CERTIFIED


# ── Retainage modes ─────────────────────────────────────────────────────────


def test_flat_retainage_uses_one_rate_for_everything(tenant: Tenant) -> None:
    tenant.contract.retainage_rule.mode = RetainageMode.FLAT
    tenant.contract.retainage_rule.rate_stored_bp = 9999  # must be ignored
    db.session.flush()

    application = _open(tenant)
    _enter(application, [(100_000_00, 50_000_00), (0, 0), (0, 0)])

    assert application.line5a_retainage_work == 10_000_00
    assert application.line5b_retainage_stored == 5_000_00


def test_variable_line_retainage_uses_the_line_rate(tenant: Tenant) -> None:
    """G703 column I: one line at 5%, the rest at the contract's 10%."""
    tenant.contract.retainage_rule.mode = RetainageMode.VARIABLE_LINE
    schedule = sov_service.approved_schedule(tenant.contract)
    schedule.lines[0].retainage_rate_bp = 500
    db.session.flush()

    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (100_000_00, 0), (0, 0)])

    assert application.lines[0].col_i_retainage == 5_000_00  # 5%
    assert application.lines[1].col_i_retainage == 10_000_00  # 10%
    assert application.line5_total_retainage == 15_000_00


def test_stepped_retainage_reduces_past_the_threshold(tenant: Tenant) -> None:
    """10% until 50% complete, 5% thereafter -- applied to the whole balance,
    which is what makes the stepped period's retainage fall."""
    rule = tenant.contract.retainage_rule
    rule.mode = RetainageMode.STEPPED
    rule.reduction_threshold_bp = 5000
    rule.reduced_rate_bp = 500
    db.session.flush()

    below = _open(tenant, month=1)
    _enter(below, [(400_000_00, 0), (0, 0), (0, 0)])  # 40% complete
    assert below.line5_total_retainage == 40_000_00  # still 10%

    app_service.submit(below, actor=tenant.user(Role.OWNER))
    db.session.commit()

    above = _open(tenant, month=2)
    _enter(above, [(100_000_00, 0), (100_000_00, 0), (0, 0)])  # 60% complete
    assert above.line4_completed_stored == 600_000_00
    assert above.line5_total_retainage == 30_000_00  # 5% of 600,000, so it falls


def test_retainage_is_summed_from_the_lines_not_the_header(tenant: Tenant) -> None:
    """The penny rule. With awkward values the per-line sum must still equal
    the header exactly -- which it does by construction, because the header
    *is* the sum."""
    application = _open(tenant)
    _enter(application, [(33_333_33, 0), (33_333_33, 0), (33_333_34, 0)])

    per_line = sum(line.col_i_retainage for line in application.lines)
    assert per_line == application.line5_total_retainage


# ── Change orders ───────────────────────────────────────────────────────────


def test_an_approved_change_order_moves_line_two_and_line_three(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))

    order = co_service.create(tenant.contract, number="CO-001", description="Added canopy")
    co_service.add_line(
        order, amount=cents(187_500_00), new_item_no="004", description="Entry canopy"
    )
    co_service.approve(order, revision, approved_date=date(2026, 3, 15))
    sov_service.approve(revision, actor=tenant.user(Role.OWNER))
    db.session.commit()

    application = _open(tenant, month=3)

    assert application.line2_net_co == 187_500_00
    assert application.line3_contract_sum_to_date == 1_187_500_00
    assert len(application.lines) == 4


def test_a_change_order_adjusts_rather_than_overwrites(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))
    target = revision.lines[0]
    original_base = target.base_scheduled_value_cents

    order = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(order, amount=cents(25_000_00), sov_line=target)
    co_service.approve(order, revision)
    db.session.commit()

    assert target.base_scheduled_value_cents == original_base, "base must not be overwritten"
    assert target.co_adjustment_cents == 25_000_00
    assert target.current_scheduled_value_cents == original_base + 25_000_00


def test_a_deductive_change_order_is_negative(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))

    order = co_service.create(tenant.contract, number="CO-002", description="Scope removed")
    co_service.add_line(order, amount=cents(-42_000_00), sov_line=revision.lines[1])
    co_service.approve(order, revision)
    db.session.commit()

    assert order.amount_cents == -42_000_00
    assert not order.is_addition
    assert co_service.approved_total(tenant.contract) == -42_000_00


def test_the_two_routes_to_line_two_agree(tenant: Tenant) -> None:
    """Sum of approved change orders, and sum of schedule adjustments. Two
    independent computations of the same number."""
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))

    first = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(first, amount=cents(50_000_00), sov_line=revision.lines[0])
    co_service.approve(first, revision)

    second = co_service.create(tenant.contract, number="CO-002")
    co_service.add_line(second, amount=cents(-12_500_00), sov_line=revision.lines[1])
    co_service.approve(second, revision)
    db.session.commit()

    assert co_service.approved_total(tenant.contract) == 37_500_00
    assert co_service.adjustment_total(revision) == 37_500_00


def test_voiding_a_change_order_backs_out_its_adjustment(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))
    target = revision.lines[0]

    keep = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(keep, amount=cents(10_000_00), sov_line=target)
    co_service.approve(keep, revision)

    undo = co_service.create(tenant.contract, number="CO-002")
    co_service.add_line(undo, amount=cents(5_000_00), sov_line=target)
    co_service.approve(undo, revision)

    co_service.void(undo, revision)
    db.session.commit()

    assert undo.status == ChangeOrderStatus.VOID
    assert target.co_adjustment_cents == 10_000_00, "the other change order must survive"


def test_a_change_order_cannot_land_on_a_frozen_schedule(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    order = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(order, amount=cents(1_000_00), sov_line=schedule.lines[0])

    with pytest.raises(ConflictError, match="Create a new revision"):
        co_service.approve(order, schedule)


def test_a_change_order_needs_a_line(tenant: Tenant) -> None:
    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))
    order = co_service.create(tenant.contract, number="CO-001")

    with pytest.raises(ValidationError, match="at least one line"):
        co_service.approve(order, revision)


def test_duplicate_change_order_numbers_are_refused(tenant: Tenant) -> None:
    co_service.create(tenant.contract, number="CO-001")
    with pytest.raises(ConflictError, match="already exists"):
        co_service.create(tenant.contract, number="CO-001")


# ── Stored materials ────────────────────────────────────────────────────────


def _store(tenant: Tenant, line_index: int, value: int, **kwargs) -> StoredMaterial:
    schedule = sov_service.approved_schedule(tenant.contract)
    material = StoredMaterial(
        organization_id=tenant.organization.id,
        sov_line_id=schedule.lines[line_index].id,
        description=kwargs.pop("description", "Switchgear"),
        value_cents=value,
        invoice_ref=kwargs.pop("invoice_ref", "INV-1001"),
        **kwargs,
    )
    db.session.add(material)
    db.session.flush()
    return material


def test_stored_material_fills_column_f(tenant: Tenant) -> None:
    _store(tenant, 0, 95_000_00)
    application = _open(tenant)
    app_service.apply_stored_materials(application)

    assert application.lines[0].col_f_stored == 95_000_00
    assert application.line4_completed_stored == 95_000_00


def test_installed_material_leaves_column_f(tenant: Tenant) -> None:
    """The double-bill trap. Once installed, the value must be billed in column
    E instead -- and column F must drop by exactly that amount."""
    material = _store(tenant, 0, 95_000_00)

    first = _open(tenant, month=1)
    app_service.apply_stored_materials(first)
    assert first.lines[0].col_f_stored == 95_000_00
    app_service.submit(first, actor=tenant.user(Role.OWNER))
    db.session.commit()

    second = _open(tenant, month=2)
    app_service.install_material(material, second)
    _enter(second, [(95_000_00, 0), (0, 0), (0, 0)])
    app_service.apply_stored_materials(second)

    assert second.lines[0].col_f_stored == 0
    assert second.lines[0].col_e_this_period == 95_000_00
    # Total to date is still 95,000 -- billed once, not twice.
    assert second.lines[0].col_g_completed_stored == 95_000_00


def test_material_cannot_be_installed_twice(tenant: Tenant) -> None:
    material = _store(tenant, 0, 10_000_00)
    application = _open(tenant)
    app_service.install_material(material, application)

    with pytest.raises(ConflictError, match="already been installed"):
        app_service.install_material(material, application)


def test_unbacked_stored_material_is_flagged(tenant: Tenant) -> None:
    _store(tenant, 0, 50_000_00, invoice_ref="")
    application = _open(tenant)
    app_service.apply_stored_materials(application)

    report = tieout.run(application)
    assert any(f.rule_id == "STORED-UNBACKED" for f in report.warnings)


def test_offsite_material_is_flagged_when_the_contract_forbids_it(tenant: Tenant) -> None:
    _store(tenant, 0, 50_000_00, location=StorageLocation.OFFSITE)
    tenant.contract.offsite_stored_allowed = False
    db.session.flush()

    application = _open(tenant)
    app_service.apply_stored_materials(application)

    report = tieout.run(application)
    assert any(f.rule_id == "STORED-OFFSITE" for f in report.warnings)


# ── Immutability ────────────────────────────────────────────────────────────


def test_a_submitted_application_cannot_be_edited(tenant: Tenant) -> None:
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.submit(application, actor=tenant.user(Role.OWNER))

    with pytest.raises(ConflictError, match="financial record"):
        _enter(application, [(200_000_00, 0), (0, 0), (0, 0)])


def test_submitting_takes_a_hashed_snapshot(tenant: Tenant) -> None:
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.submit(application, actor=tenant.user(Role.OWNER))

    assert application.snapshot is not None
    assert len(application.snapshot.sha256) == 64
    assert '"line8_current_payment_due":9000000' in application.snapshot.payload


def test_a_later_schedule_revision_does_not_restate_a_submitted_period(
    tenant: Tenant,
) -> None:
    """The durability promise: the snapshot must still describe what was
    submitted after the schedule of values has moved on."""
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    original = application.snapshot.payload
    db.session.commit()

    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))
    order = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(order, amount=cents(500_000_00), sov_line=revision.lines[0])
    co_service.approve(order, revision)
    db.session.commit()

    assert application.snapshot.payload == original
    assert application.line3_contract_sum_to_date == 1_000_000_00


def test_a_paid_application_cannot_be_voided(tenant: Tenant) -> None:
    application = _open(tenant)
    _enter(application, [(100_000_00, 0), (0, 0), (0, 0)])
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    application.status = ApplicationStatus.PAID
    db.session.flush()

    with pytest.raises(ConflictError, match="cannot be voided"):
        app_service.void(application, reason="mistake")


# ── Retainage service directly ──────────────────────────────────────────────


def test_effective_rates_step_down_at_the_threshold(app: Flask) -> None:
    from massingbill.models import RetainageRule

    rule = RetainageRule(
        organization_id="x",
        mode=RetainageMode.STEPPED,
        rate_work_bp=1000,
        rate_stored_bp=1000,
        reduction_threshold_bp=5000,
        reduced_rate_bp=500,
    )

    below, _ = retainage.effective_rates(
        rule, completed_stored=cents(400_000), contract_sum=cents(1_000_000)
    )
    at, _ = retainage.effective_rates(
        rule, completed_stored=cents(500_000), contract_sum=cents(1_000_000)
    )

    assert below == bp(1000)
    assert at == bp(500), "the threshold is inclusive"


def test_a_variable_line_without_a_rate_falls_back_to_the_contract_rate(app: Flask) -> None:
    """Silently withholding nothing because a field was left blank is the wrong
    failure direction."""
    from massingbill.models import RetainageRule

    rule = RetainageRule(
        organization_id="x",
        mode=RetainageMode.VARIABLE_LINE,
        rate_work_bp=1000,
        rate_stored_bp=1000,
    )
    result = retainage.compute(
        rule,
        [retainage.LineBasis(cents(100_000), cents(100_000), cents(0), line_rate_bp=None)],
        cents(100_000),
    )
    assert result.total == 10_000
