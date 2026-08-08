"""The tie-out rule engine: one passing and one failing case per rule.

A rule that has never been seen to fire is a rule nobody knows works. Each test
below breaks exactly one thing and asserts that exactly that rule reports it.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import ApplicationStatus, Role, StorageLocation, StoredMaterial
from massingbill.services import application as app_service
from massingbill.services import change_order as co_service
from massingbill.services import sov as sov_service
from massingbill.services import tieout
from massingbill.services.money import cents
from massingbill.services.tieout import Severity
from tests.factories import Tenant, make_tenant


def _project(app: Flask) -> Tenant:
    tenant = make_tenant("tie", contract_sum_cents=1_000_000_00)
    for item, value in (("001", 600_000_00), ("002", 400_000_00)):
        sov_service.add_line(
            tenant.schedule,
            sov_service.LineInput(
                item_no=item, description=f"Line {item}", scheduled_value_cents=cents(value)
            ),
            actor=tenant.user(Role.OWNER),
        )
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()
    return tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return _project(app)


@pytest.fixture
def application(tenant: Tenant):
    built = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
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


def _ids(report: tieout.TieoutReport) -> set[str]:
    return {finding.rule_id for finding in report.findings}


# ── The clean case ──────────────────────────────────────────────────────────


def test_a_correct_application_reports_nothing_blocking(application) -> None:
    report = tieout.run(application)

    assert report.ok
    assert report.blocking == []
    assert report.warnings == []
    assert report.informational, "informational findings should always be present"
    assert report.summary() == "Every check passed. The application ties."


def test_every_finding_serialises(application) -> None:
    for finding in tieout.run(application).findings:
        payload = finding.as_dict()
        assert payload["rule_id"]
        assert payload["severity"] in {"error", "warning", "info"}
        assert payload["message"]


# ── Structural rules ────────────────────────────────────────────────────────


def test_sov_001_fires_when_the_lines_do_not_total_the_contract_sum(application) -> None:
    application.lines[0].col_c_scheduled_value += 1_00
    db.session.flush()

    report = tieout.run(application)
    assert "SOV-001" in _ids(report)
    assert not report.ok


def test_g702_002_fires_when_line_two_disagrees_with_the_change_orders(
    tenant: Tenant, application
) -> None:
    application.line2_net_co = 50_000_00
    db.session.flush()

    assert "G702-002" in _ids(tieout.run(application))


def test_g702_003_fires_when_line_three_is_not_one_plus_two(application) -> None:
    application.line3_contract_sum_to_date += 1
    db.session.flush()

    assert "G702-003" in _ids(tieout.run(application))


def test_g702_004_fires_when_line_four_is_not_the_column_g_total(application) -> None:
    application.line4_completed_stored += 1
    db.session.flush()

    assert "G702-004" in _ids(tieout.run(application))


def test_g702_005_fires_when_retainage_does_not_add_up(application) -> None:
    application.line5_total_retainage += 1
    db.session.flush()

    assert "G702-005" in _ids(tieout.run(application))


def test_g702_006_fires_when_line_six_is_not_four_less_five(application) -> None:
    application.line6_earned_less_retainage += 1
    db.session.flush()

    assert "G702-006" in _ids(tieout.run(application))


def test_g702_007_fires_when_line_seven_is_not_the_previous_certificate(
    tenant: Tenant, application
) -> None:
    application.line7_previous_certificates = 5_000_00
    db.session.flush()

    assert "G702-007" in _ids(tieout.run(application))


def test_g702_008_fires_when_line_eight_is_not_six_less_seven(application) -> None:
    application.line8_current_payment_due += 1
    db.session.flush()

    assert "G702-008" in _ids(tieout.run(application))


def test_g702_009_fires_when_line_nine_is_not_three_less_six(application) -> None:
    application.line9_balance_to_finish += 1
    db.session.flush()

    assert "G702-009" in _ids(tieout.run(application))


def test_co_sum_fires_when_the_summary_box_does_not_net_to_line_two(application) -> None:
    application.co_summary_this_additions = 10_000_00
    db.session.flush()

    assert "CO-SUM" in _ids(tieout.run(application))


def test_penny_fires_when_line_retainage_does_not_sum_to_the_header(application) -> None:
    """The one-cent rule. This is the discrepancy that gets pay apps rejected."""
    application.lines[0].col_i_retainage += 1
    db.session.flush()

    report = tieout.run(application)
    assert "PENNY" in _ids(report)
    assert not report.ok


# ── Line-level rules ────────────────────────────────────────────────────────


def test_g703_g_fires_when_column_g_is_not_d_plus_e_plus_f(application) -> None:
    application.lines[0].col_g_completed_stored += 1
    db.session.flush()

    finding = next(f for f in tieout.run(application).findings if f.rule_id == "G703-G")
    assert finding.line_item == application.lines[0].item_no


def test_g703_h_fires_when_column_h_is_not_c_less_g(application) -> None:
    application.lines[0].col_h_balance += 1
    db.session.flush()

    assert "G703-H" in _ids(tieout.run(application))


def test_g703_d_fires_when_column_d_does_not_carry_forward(tenant: Tenant, application) -> None:
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()

    second = app_service.open_period(
        tenant.contract, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )
    second.lines[0].col_d_previous = 0  # as if someone retyped it
    db.session.flush()

    assert "G703-D" in _ids(tieout.run(second))


# ── Policy rules ────────────────────────────────────────────────────────────


def test_overbill_warns_when_a_line_exceeds_its_scheduled_value(
    tenant: Tenant, application
) -> None:
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(application.lines, [700_000_00, 0], strict=True)
        ],
    )

    report = tieout.run(application)
    assert "OVERBILL" in _ids(report)
    assert "PCT-OVER" in _ids(report)
    assert report.ok, "overbilling is contestable, not arithmetically wrong"


def test_negative_period_warns(tenant: Tenant, application) -> None:
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(application.lines, [-10_000_00, 100_000_00], strict=True)
        ],
    )

    assert "NEGATIVE-PERIOD" in _ids(tieout.run(application))


def test_retain_cap_warns_when_withholding_exceeds_the_statute(tenant: Tenant, application) -> None:
    """California SB 61 capped private-works retention at 5% from 2026-01-01."""
    rule = tenant.contract.retainage_rule
    rule.statutory_cap_bp = 500
    rule.statute_citation = "Cal. Civ. Code 8812 (SB 61, eff. 2026-01-01)"
    db.session.flush()
    app_service.recompute(application)

    report = tieout.run(application)
    finding = next(f for f in report.findings if f.rule_id == "RETAIN-CAP")

    assert finding.severity == Severity.WARNING
    assert "SB 61" in finding.message


def test_retain_cap_blocks_when_the_rule_says_block(tenant: Tenant, application) -> None:
    rule = tenant.contract.retainage_rule
    rule.statutory_cap_bp = 500
    rule.cap_enforcement = "block"
    db.session.flush()
    app_service.recompute(application)

    report = tieout.run(application)
    assert not report.ok
    assert any(f.rule_id == "RETAIN-CAP" for f in report.blocking)


def test_retain_cap_stays_quiet_within_the_cap(tenant: Tenant, application) -> None:
    rule = tenant.contract.retainage_rule
    rule.statutory_cap_bp = 1000  # exactly the contract rate
    db.session.flush()
    app_service.recompute(application)

    assert "RETAIN-CAP" not in _ids(tieout.run(application))


def test_sequence_warns_on_a_numbering_gap(tenant: Tenant, application) -> None:
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()

    second = app_service.open_period(
        tenant.contract, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )
    second.number = 5
    db.session.flush()

    assert "SEQUENCE" in _ids(tieout.run(second))


def test_stored_double_blocks_when_installed_material_is_still_in_column_f(
    tenant: Tenant, application
) -> None:
    """The double-bill trap, as a blocking rule."""
    schedule = sov_service.approved_schedule(tenant.contract)
    material = StoredMaterial(
        organization_id=tenant.organization.id,
        sov_line_id=schedule.lines[0].id,
        description="Switchgear",
        value_cents=50_000_00,
        invoice_ref="INV-1",
    )
    db.session.add(material)
    db.session.flush()

    app_service.install_material(material, application)
    application.lines[0].col_f_stored = 50_000_00  # as if F was never reduced
    db.session.flush()

    report = tieout.run(application)
    assert "STORED-DOUBLE" in _ids(report)
    assert not report.ok


def test_stored_unbonded_warns_for_offsite_material_without_a_bond(
    tenant: Tenant, application
) -> None:
    tenant.contract.offsite_stored_allowed = True
    tenant.contract.bonding_required_for_stored = True
    schedule = sov_service.approved_schedule(tenant.contract)
    db.session.add(
        StoredMaterial(
            organization_id=tenant.organization.id,
            sov_line_id=schedule.lines[0].id,
            description="Generator",
            value_cents=40_000_00,
            invoice_ref="INV-2",
            location=StorageLocation.OFFSITE,
        )
    )
    db.session.flush()
    app_service.apply_stored_materials(application)

    assert "STORED-UNBONDED" in _ids(tieout.run(application))


# ── Snapshot rules ──────────────────────────────────────────────────────────


def test_snapshot_drift_fires_when_a_submitted_application_is_altered(
    tenant: Tenant, application
) -> None:
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert tieout.run(application).ok

    application.line8_current_payment_due += 1_00  # tamper
    db.session.flush()

    report = tieout.run(application)
    assert "SNAPSHOT-DRIFT" in _ids(report)
    assert not report.ok


def test_snapshot_missing_fires_when_an_issued_application_has_none(
    tenant: Tenant, application
) -> None:
    application.status = ApplicationStatus.SUBMITTED
    db.session.flush()

    assert "SNAPSHOT-MISSING" in _ids(tieout.run(application))


def test_a_frozen_application_is_not_rechecked_against_live_data(
    tenant: Tenant, application
) -> None:
    """A submitted application must not start failing because the world moved.

    Its change-order log grows, its schedule is revised, later periods are
    certified. None of that makes the issued document wrong.
    """
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()

    schedule = sov_service.approved_schedule(tenant.contract)
    revision = sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))
    order = co_service.create(tenant.contract, number="CO-001")
    co_service.add_line(order, amount=cents(250_000_00), sov_line=revision.lines[0])
    co_service.approve(order, revision)
    db.session.commit()

    report = tieout.run(application)
    assert report.ok, (
        "a frozen application must not be re-checked against a moved world: "
        + "; ".join(f.rule_id for f in report.blocking)
    )


# ── Submission gating ───────────────────────────────────────────────────────


def test_submission_is_refused_when_the_application_does_not_tie(
    tenant: Tenant, application
) -> None:
    application.line8_current_payment_due += 1
    db.session.flush()

    with pytest.raises(ValidationError, match="does not tie out"):
        app_service.submit(application, actor=tenant.user(Role.OWNER))

    assert application.status == ApplicationStatus.DRAFT


def test_submission_proceeds_despite_warnings(tenant: Tenant, application) -> None:
    """A warning travels with the package; it does not stop the month."""
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(application.lines, [700_000_00, 0], strict=True)
        ],
    )
    assert tieout.run(application).warnings

    app_service.submit(application, actor=tenant.user(Role.OWNER))
    assert application.status == ApplicationStatus.SUBMITTED


# ── Informational ───────────────────────────────────────────────────────────


def test_informational_findings_describe_the_period(application) -> None:
    ids = _ids(tieout.run(application))

    assert "INFO-RETAINED" in ids
    assert "INFO-COMPLETE" in ids


def test_movement_is_reported_against_the_previous_period(tenant: Tenant, application) -> None:
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()

    second = app_service.open_period(
        tenant.contract, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )
    app_service.enter(
        second,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(second.lines, [100_000_00, 0], strict=True)
        ],
    )

    assert "INFO-MOVEMENT" in _ids(tieout.run(second))
