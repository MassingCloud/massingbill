"""Compliance documents: currency, blocking, and recurrence.

The distinction under test is between *warning* and *refusing*. A GC who has
agreed to hold funds until certified payroll is in hand needs the system to
refuse, or the agreement is decorative.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import ComplianceKind, Role
from massingbill.services import application as app_service
from massingbill.services import compliance as compliance_service
from massingbill.services import sov as sov_service
from massingbill.services import tieout
from massingbill.services.money import cents
from tests.factories import Tenant, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("comply", contract_sum_cents=1_000_000_00)
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
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        actor=tenant.user(Role.OWNER),
    )
    app_service.enter(
        built,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(value), stored=cents(0))
            for line, value in zip(built.lines, [200_000_00, 100_000_00], strict=True)
        ],
    )
    return built


def _ids(application) -> set[str]:
    return {f.rule_id for f in tieout.run(application).findings}


# ── Evaluation ──────────────────────────────────────────────────────────────


def test_no_requirements_means_nothing_to_report(tenant: Tenant, application) -> None:
    assert compliance_service.evaluate(application) == []
    assert not any(rule.startswith("COMPLIANCE-") for rule in _ids(application))


def test_a_missing_required_document_is_reported(tenant: Tenant, application) -> None:
    compliance_service.add_requirement(tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE)
    db.session.flush()

    states = compliance_service.evaluate(application)
    assert len(states) == 1
    assert not states[0].satisfied
    assert states[0].reason == "nothing on file"
    assert states[0].blocks


def test_a_current_document_satisfies(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        requirement,
        filename="coi-2026.pdf",
        effective_from=date(2026, 1, 1),
        expires_on=date(2027, 1, 1),
    )
    db.session.flush()

    state = compliance_service.evaluate(application)[0]
    assert state.satisfied
    assert not state.blocks


def test_currency_is_judged_as_at_the_period_end_not_today(tenant: Tenant, application) -> None:
    """A certificate that has lapsed since does not retroactively make last
    quarter's work uninsured, and failing the old application for it is wrong."""
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        requirement,
        filename="coi.pdf",
        effective_from=date(2026, 1, 1),
        expires_on=date(2026, 7, 31),  # after the June period, before "now"
    )
    db.session.flush()

    assert compliance_service.evaluate(application)[0].satisfied


def test_a_lapsed_document_says_when_it_expired(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(requirement, filename="old.pdf", expires_on=date(2026, 3, 1))
    db.session.flush()

    state = compliance_service.evaluate(application)[0]
    assert not state.satisfied
    assert "expired 2026-03-01" in state.reason


def test_a_voided_document_does_not_satisfy(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    document = compliance_service.file_document(
        requirement, filename="coi.pdf", expires_on=date(2027, 1, 1)
    )
    document.is_void = True
    db.session.flush()

    assert not compliance_service.evaluate(application)[0].satisfied


def test_a_document_not_yet_effective_does_not_satisfy(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        requirement, filename="future.pdf", effective_from=date(2027, 1, 1)
    )
    db.session.flush()

    assert not compliance_service.evaluate(application)[0].satisfied


# ── Recurrence ──────────────────────────────────────────────────────────────


def test_a_recurring_requirement_needs_a_document_for_this_period(
    tenant: Tenant, application
) -> None:
    """Last month's certified payroll says nothing about this month's."""
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFIED_PAYROLL, is_recurring=True
    )
    compliance_service.file_document(requirement, filename="may-payroll.pdf")
    db.session.flush()

    state = compliance_service.evaluate(application)[0]
    assert not state.satisfied
    assert state.reason == "nothing on file for this period"


def test_a_recurring_requirement_is_satisfied_by_this_period_document(
    tenant: Tenant, application
) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFIED_PAYROLL, is_recurring=True
    )
    compliance_service.file_document(
        requirement, filename="june-payroll.pdf", application=application
    )
    db.session.flush()

    assert compliance_service.evaluate(application)[0].satisfied


def test_a_one_off_requirement_is_satisfied_once(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.W9, is_recurring=False
    )
    compliance_service.file_document(requirement, filename="w9.pdf")
    db.session.flush()

    assert compliance_service.evaluate(application)[0].satisfied


# ── Blocking and warning ────────────────────────────────────────────────────


def test_a_blocking_requirement_refuses_submission(tenant: Tenant, application) -> None:
    compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFIED_PAYROLL, blocks_payment=True
    )
    db.session.flush()

    report = tieout.run(application)
    assert "COMPLIANCE-MISSING" in {f.rule_id for f in report.blocking}
    assert not report.ok


def test_a_non_blocking_requirement_only_warns(tenant: Tenant, application) -> None:
    compliance_service.add_requirement(tenant.project, ComplianceKind.W9, blocks_payment=False)
    db.session.flush()

    report = tieout.run(application)
    assert "COMPLIANCE-MISSING" in {f.rule_id for f in report.warnings}
    assert report.ok


def test_submission_is_actually_refused_when_a_document_is_missing(
    tenant: Tenant, application
) -> None:
    from massingbill.errors import ValidationError

    compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE, blocks_payment=True
    )
    db.session.flush()

    with pytest.raises(ValidationError, match="does not tie out"):
        app_service.submit(application, actor=tenant.user(Role.OWNER))


def test_an_expiring_document_warns_before_it_bites(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        requirement,
        filename="coi.pdf",
        effective_from=date(2026, 1, 1),
        expires_on=date(2026, 7, 15),  # 15 days after the June period end
    )
    db.session.flush()

    states = compliance_service.evaluate(application)
    assert states[0].satisfied
    assert states[0].expiring_soon
    assert "COMPLIANCE-EXPIRING" in _ids(application)


def test_a_document_with_plenty_of_runway_does_not_warn(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        requirement,
        filename="coi.pdf",
        effective_from=date(2026, 1, 1),
        expires_on=date(2027, 6, 30),
    )
    db.session.flush()

    assert "COMPLIANCE-EXPIRING" not in _ids(application)


def test_a_document_with_no_expiry_never_warns(tenant: Tenant, application) -> None:
    requirement = compliance_service.add_requirement(tenant.project, ComplianceKind.W9)
    compliance_service.file_document(requirement, filename="w9.pdf")
    db.session.flush()

    state = compliance_service.evaluate(application)[0]
    assert state.satisfied
    assert state.expires_in_days is None
    assert not state.expiring_soon


def test_blocking_and_expiring_helpers_agree_with_evaluate(tenant: Tenant, application) -> None:
    compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFIED_PAYROLL, blocks_payment=True
    )
    soon = compliance_service.add_requirement(
        tenant.project, ComplianceKind.CERTIFICATE_OF_INSURANCE
    )
    compliance_service.file_document(
        soon, filename="coi.pdf", effective_from=date(2026, 1, 1), expires_on=date(2026, 7, 10)
    )
    db.session.flush()

    assert len(compliance_service.blocking(application)) == 1
    assert len(compliance_service.expiring(application)) == 1
