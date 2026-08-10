"""The demo project.

It is published on the project's own web page, so it is a public claim about
correctness. These tests hold it to that: every period must tie out, and the
figures a reader can check by hand must be the ones the engine produces.
"""

from __future__ import annotations

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import ApplicationStatus, WaiverTemplate
from massingbill.services import application as app_service
from massingbill.services import demo as demo_service
from massingbill.services import payments as payment_service
from massingbill.services import subcontracts as sub_service
from massingbill.services import tieout


@pytest.fixture
def demo(app: Flask):
    return demo_service.build()


def test_the_demo_builds(demo) -> None:
    assert demo.project.number == "2026-014"
    assert demo.project.jurisdiction_state == "CA"
    assert demo.contract.original_contract_sum_cents == demo_service.CONTRACT_SUM


def test_it_runs_six_periods(demo) -> None:
    applications = app_service.applications_for(demo.contract)
    assert len(applications) == 6
    assert all(a.status != ApplicationStatus.DRAFT for a in applications)


def test_every_period_ties_out(demo) -> None:
    """The demo is published. Shipping one that does not balance would be worse
    than shipping nothing."""
    for application in app_service.applications_for(demo.contract):
        report = tieout.run(application)
        assert report.ok, f"demo application #{application.number} failed tie-out: " + "; ".join(
            f.rule_id for f in report.blocking
        )


def test_the_change_order_moves_the_contract_sum(demo) -> None:
    applications = app_service.applications_for(demo.contract)

    # CO-001 adds $148,000 in period 3; CO-002 deducts $62,000 in period 6.
    assert applications[0].line3_contract_sum_to_date == demo_service.CONTRACT_SUM
    assert applications[3].line3_contract_sum_to_date == demo_service.CONTRACT_SUM + 148_000_00
    assert applications[5].line3_contract_sum_to_date == (
        demo_service.CONTRACT_SUM + 148_000_00 - 62_000_00
    )


def test_california_retention_is_held_at_five_percent(demo) -> None:
    """SB 61 caps private-works retention at 5% from 2026-01-01, so the demo
    starts there rather than at the more common 10%."""
    first = app_service.applications_for(demo.contract)[0]

    assert first.line4_completed_stored == 347_000_00
    assert first.line5a_retainage_work == 17_350_00  # 5%
    assert first.line8_current_payment_due == 329_650_00


def test_stored_material_is_billed_once_not_twice(demo) -> None:
    """The trap the whole stored-material model exists to prevent: $245,000 of
    curtain wall sits in column F, then installs into column E, and the line's
    total to date does not move."""
    applications = app_service.applications_for(demo.contract)
    stored_period, installed_period = applications[3], applications[4]

    before = next(line for line in stored_period.lines if line.item_no == "008")
    after = next(line for line in installed_period.lines if line.item_no == "008")

    assert before.col_f_stored == 245_000_00
    assert before.col_e_this_period == 0

    assert after.col_f_stored == 0, "installed material must leave column F"
    assert after.col_e_this_period == 245_000_00
    assert after.col_g_completed_stored == before.col_g_completed_stored


def test_the_architect_certifies_less_in_the_final_period(demo) -> None:
    final = app_service.applications_for(demo.contract)[-1]

    assert final.certification is not None
    assert final.certification.variance_cents == -18_000_00
    assert final.certified_payment_cents == final.line8_current_payment_due - 18_000_00


def test_the_statutory_waiver_refuses_to_render(demo) -> None:
    """California prescribes its waiver wording, so the demo cannot issue one --
    and the refusal is the feature, not an obstacle."""
    assert demo.waiver_refusal
    assert "has not been verified" in demo.waiver_refusal
    assert "Cal. Civ. Code" in demo.waiver_refusal


def test_the_statutory_templates_ship_empty(demo) -> None:
    statutory = list(
        db.session.scalars(
            db.select(WaiverTemplate).where(
                WaiverTemplate.organization_id == demo.organization.id,
                WaiverTemplate.is_statutory.is_(True),
            )
        )
    )
    assert len(statutory) == 48
    assert all(not template.is_usable for template in statutory)


def test_a_payment_is_recorded_and_closes_its_period(demo) -> None:
    applications = app_service.applications_for(demo.contract)
    paid = applications[-2]

    assert paid.status == ApplicationStatus.PAID
    assert payment_service.paid_to_date(paid) == paid.certified_payment_cents
    assert payment_service.variance(paid) == 0


def test_the_subcontracts_are_committed(demo) -> None:
    subs = sub_service.for_project(demo.project)

    assert [s.number for s in subs] == ["SC-001", "SC-002", "SC-003"]
    assert sub_service.committed_total(demo.project) == 1_230_000_00


def test_compliance_is_on_file(demo) -> None:
    from massingbill.services import compliance as compliance_service

    latest = app_service.applications_for(demo.contract)[-1]
    states = compliance_service.evaluate(latest)

    # The certificate of insurance is current; the W-9 is outstanding but does
    # not block, which is what makes the demo show both behaviours.
    assert any(state.satisfied for state in states)
    assert not compliance_service.blocking(latest)


def test_the_audit_chain_is_intact(demo) -> None:
    from massingbill.services import audit

    verdict = audit.verify(demo.organization.id)
    assert verdict.ok
    assert verdict.events > 40
