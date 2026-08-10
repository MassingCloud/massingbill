"""Lien waivers: form selection, the refusal, signatures, and amount checking.

The refusal is the important one. Twelve states prescribe waiver wording, and a
form that does not substantially conform can be unenforceable -- so this suite
proves the engine will not issue a statutory waiver built from text nobody has
verified.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import Role, WaiverStatus, WaiverTemplate, WaiverType
from massingbill.services import application as app_service
from massingbill.services import sov as sov_service
from massingbill.services import tieout
from massingbill.services import waivers as waiver_service
from massingbill.services.money import cents
from tests.factories import Tenant, make_tenant


def _billable(app: Flask, *, state: str = "NY", residential: bool = False) -> Tenant:
    tenant = make_tenant("waive", contract_sum_cents=1_000_000_00)
    tenant.project.jurisdiction_state = state
    tenant.project.is_residential = residential
    for item, value in (("001", 600_000_00), ("002", 400_000_00)):
        sov_service.add_line(
            tenant.schedule,
            sov_service.LineInput(
                item_no=item, description=f"Line {item}", scheduled_value_cents=cents(value)
            ),
            actor=tenant.user(Role.OWNER),
        )
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    waiver_service.seed_templates(tenant.organization)
    db.session.commit()
    return tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return _billable(app)


@pytest.fixture
def application(tenant: Tenant):
    built = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
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


# ── Seeding ─────────────────────────────────────────────────────────────────


def test_seeding_loads_the_general_and_statutory_forms(tenant: Tenant) -> None:
    templates = list(
        db.session.scalars(
            db.select(WaiverTemplate).where(
                WaiverTemplate.organization_id == tenant.organization.id
            )
        )
    )

    assert len(templates) == 4 + (12 * 4)  # general + twelve states x four types
    assert {t.state for t in templates} >= {"", "CA", "TX", "GA", "MI", "AZ", "FL", "MO"}


def test_seeding_twice_adds_nothing(tenant: Tenant) -> None:
    assert waiver_service.seed_templates(tenant.organization) == 0


def test_the_general_forms_are_verified_and_usable(tenant: Tenant) -> None:
    for waiver_type in WaiverType:
        template = waiver_service.template_for(
            tenant.organization.id, state="NY", waiver_type=waiver_type, on=date(2026, 5, 31)
        )
        assert template.state == ""
        assert template.is_usable


def test_every_statutory_form_ships_unverified(tenant: Tenant) -> None:
    """The whole point. Inventing statutory language would release lien rights
    nobody would discover were gone until the money was."""
    unverified = waiver_service.unverified_templates(tenant.organization.id)

    assert len(unverified) == 48
    assert all(not t.is_usable for t in unverified)
    assert all(t.citation for t in unverified), "each must say which statute to read"


def test_statutory_forms_carry_their_citation(tenant: Tenant) -> None:
    california = waiver_service.template_for(
        tenant.organization.id,
        state="CA",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 5, 31),
    )
    assert "Cal. Civ. Code" in california.citation


def test_arizona_requires_notarisation(tenant: Tenant) -> None:
    arizona = waiver_service.template_for(
        tenant.organization.id,
        state="AZ",
        waiver_type=WaiverType.UNCONDITIONAL_FINAL,
        on=date(2026, 5, 31),
    )
    assert arizona.notary_required


# ── Selecting a form ────────────────────────────────────────────────────────


def test_a_state_with_no_prescribed_form_gets_the_general_one(tenant: Tenant) -> None:
    template = waiver_service.template_for(
        tenant.organization.id,
        state="NY",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 5, 31),
    )
    assert template.state == ""
    assert not template.is_statutory


def test_a_prescribed_form_state_gets_its_own(tenant: Tenant) -> None:
    template = waiver_service.template_for(
        tenant.organization.id,
        state="TX",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 5, 31),
    )
    assert template.state == "TX"
    assert template.is_statutory


def test_missouri_uses_the_statutory_form_only_for_residential(tenant: Tenant) -> None:
    """Mo. Rev. Stat. 429.016 applies to residential work; commercial may use
    the general form."""
    commercial = waiver_service.template_for(
        tenant.organization.id,
        state="MO",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 5, 31),
        is_residential=False,
    )
    residential = waiver_service.template_for(
        tenant.organization.id,
        state="MO",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 5, 31),
        is_residential=True,
    )

    assert commercial.state == ""
    assert residential.state == "MO"


def test_a_form_outside_its_effective_window_is_not_selected(tenant: Tenant) -> None:
    with pytest.raises(NotFoundError, match="No conditional waiver"):
        waiver_service.template_for(
            tenant.organization.id,
            state="NY",
            waiver_type=WaiverType.CONDITIONAL_PROGRESS,
            on=date(1990, 1, 1),
        )


def test_the_newest_form_in_force_wins(tenant: Tenant) -> None:
    """A statute that is amended leaves both versions on file; a waiver renders
    the one in force on the period it releases."""
    amended = WaiverTemplate(
        organization_id=tenant.organization.id,
        state="",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        title="General conditional progress (2026 revision)",
        body=(
            "Amended text {{ claimant }} {{ customer }} {{ project }} "
            "{{ amount }} {{ through_date }}"
        ),
        required_fields="claimant,customer,project,amount,through_date",
        verified=True,
        effective_from=date(2026, 1, 1),
    )
    db.session.add(amended)
    db.session.flush()

    before = waiver_service.template_for(
        tenant.organization.id,
        state="NY",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2025, 6, 1),
    )
    after = waiver_service.template_for(
        tenant.organization.id,
        state="NY",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=date(2026, 6, 1),
    )

    assert before.effective_from < after.effective_from
    assert after.id == amended.id


# ── The refusal ─────────────────────────────────────────────────────────────


def test_an_unverified_statutory_form_refuses_to_render(tenant: Tenant, application) -> None:
    """Being unable to issue a waiver is recoverable in an afternoon. Issuing
    one built from invented statutory text is not recoverable at all."""
    tenant.project.jurisdiction_state = "CA"
    db.session.flush()

    with pytest.raises(ConflictError) as exc:
        waiver_service.request(
            application,
            waiver_type=WaiverType.CONDITIONAL_PROGRESS,
            claimant="Acme Construction",
            customer="Riverside Owner LLC",
            amount=cents(100_000_00),
        )

    message = str(exc.value)
    assert "has not been verified" in message
    assert "Cal. Civ. Code" in message, "the refusal must say which statute to read"


def test_verifying_a_template_makes_it_usable(tenant: Tenant, application) -> None:
    tenant.project.jurisdiction_state = "TX"
    db.session.flush()

    template = waiver_service.template_for(
        tenant.organization.id,
        state="TX",
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        on=application.period_end,
    )
    waiver_service.verify_template(
        template,
        body="Verbatim statutory text for {{ claimant }} on {{ project }} "
        "through {{ through_date }} for {{ amount }} from {{ customer }}.",
        actor=tenant.user(Role.OWNER),
    )

    waiver = waiver_service.request(
        application,
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        claimant="Acme Construction",
        customer="Riverside Owner LLC",
        amount=cents(100_000_00),
    )
    assert "Verbatim statutory text" in waiver.rendered_body


def test_verifying_with_empty_text_is_refused(tenant: Tenant) -> None:
    template = waiver_service.unverified_templates(tenant.organization.id)[0]
    with pytest.raises(ValidationError, match="needs its text"):
        waiver_service.verify_template(template, body="   ")


def test_a_template_body_cannot_reach_application_internals(tenant: Tenant) -> None:
    """Statutory text is entered by an administrator and then rendered as a
    template, which without a sandbox is server-side template injection by
    whoever transcribes the statute."""
    from jinja2.exceptions import SecurityError

    # Built in memory rather than inserted: the point is the renderer, and the
    # table's uniqueness constraint would (correctly) reject a second general
    # form with the same effective date.
    template = WaiverTemplate(
        organization_id=tenant.organization.id,
        state="",
        waiver_type=WaiverType.CONDITIONAL_FINAL,
        title="Hostile",
        body="{{ ''.__class__.__mro__[1].__subclasses__() }}",
        required_fields="",
        verified=True,
        effective_from=date(2000, 1, 1),
    )

    fields = waiver_service.WaiverFields(
        claimant="Acme",
        customer="Owner",
        project="P",
        amount=cents(100),
        through_date=date(2026, 5, 31),
    )
    with pytest.raises(SecurityError):
        waiver_service.render_body(template, fields)


# ── Rendering ───────────────────────────────────────────────────────────────


def test_a_requested_waiver_renders_the_form(tenant: Tenant, application) -> None:
    waiver = waiver_service.request(
        application,
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        claimant="Acme Construction",
        customer="Riverside Owner LLC",
        amount=cents(270_000_00),
    )

    assert waiver.status == WaiverStatus.REQUESTED
    assert "Acme Construction" in waiver.rendered_body
    assert "$270,000.00" in waiver.rendered_body
    assert waiver.through_date == application.period_end
    assert len(waiver.rendered_sha256) == 64


def test_a_conditional_waiver_says_it_depends_on_payment(tenant: Tenant, application) -> None:
    waiver = waiver_service.request(
        application,
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        claimant="Acme",
        customer="Owner",
        amount=cents(1_000_00),
    )
    assert "effective only on the claimant's receipt of payment" in waiver.rendered_body


def test_an_unconditional_waiver_carries_the_warning(tenant: Tenant, application) -> None:
    """The notice is what stops someone signing away rights before the cheque
    clears, so its absence would be a defect."""
    waiver = waiver_service.request(
        application,
        waiver_type=WaiverType.UNCONDITIONAL_PROGRESS,
        claimant="Acme",
        customer="Owner",
        amount=cents(1_000_00),
    )
    # Normalised for the assertion only. The stored body keeps its authored
    # line breaks, which matters for the statutory forms: several states require
    # the notice to appear as written, so the renderer must not reflow it.
    notice = " ".join(waiver.rendered_body.split())

    assert "WAIVES THE CLAIMANT'S LIEN" in notice
    assert "A PERSON SHOULD NOT RELY ON THIS DOCUMENT UNLESS SATISFIED" in notice


def test_a_missing_required_field_is_refused(tenant: Tenant, application) -> None:
    with pytest.raises(ValidationError, match="requires"):
        waiver_service.request(
            application,
            waiver_type=WaiverType.CONDITIONAL_PROGRESS,
            claimant="",
            customer="Owner",
            amount=cents(1_000_00),
        )


# ── Signatures ──────────────────────────────────────────────────────────────


def _waiver(application, amount: int = 270_000_00, **kwargs):
    return waiver_service.request(
        application,
        waiver_type=kwargs.pop("waiver_type", WaiverType.CONDITIONAL_PROGRESS),
        claimant="Acme Construction",
        customer="Riverside Owner LLC",
        amount=cents(amount),
        **kwargs,
    )


def test_signing_records_the_evidence(tenant: Tenant, application) -> None:
    waiver = _waiver(application)
    signature = waiver_service.sign(
        waiver,
        signer_name="Dana Reyes",
        signer_title="Controller",
        signer_email="dana@acme.example",
        consented=True,
        ip="203.0.113.7",
        user_agent="Mozilla/5.0",
    )

    assert waiver.status == WaiverStatus.SIGNED
    assert signature.document_sha256 == waiver.rendered_sha256
    assert signature.consented
    assert "same legal effect as a handwritten one" in signature.consent_text
    assert signature.ip == "203.0.113.7"


def test_signing_without_consent_is_refused(tenant: Tenant, application) -> None:
    """ESIGN and UETA both turn on affirmative consent, recorded."""
    with pytest.raises(ValidationError, match="recorded consent"):
        waiver_service.sign(_waiver(application), signer_name="Dana", consented=False)


def test_signing_without_a_name_is_refused(tenant: Tenant, application) -> None:
    with pytest.raises(ValidationError, match="signer's name"):
        waiver_service.sign(_waiver(application), signer_name="  ", consented=True)


def test_a_waiver_cannot_be_signed_twice(tenant: Tenant, application) -> None:
    waiver = _waiver(application)
    waiver_service.sign(waiver, signer_name="Dana", consented=True)

    with pytest.raises(ConflictError, match="cannot be signed again"):
        waiver_service.sign(waiver, signer_name="Someone Else", consented=True)


def test_the_signature_binds_the_exact_document(tenant: Tenant, application) -> None:
    """Editing a signed waiver must detach the signature rather than silently
    carrying it over to different words."""
    waiver = _waiver(application)
    waiver_service.sign(waiver, signer_name="Dana", consented=True)
    assert waiver_service.signature_is_intact(waiver)

    waiver.rendered_body += "\n\nAnd also everything else."
    db.session.flush()

    assert not waiver_service.signature_is_intact(waiver)


def test_notarising_requires_a_signature_first(tenant: Tenant, application) -> None:
    with pytest.raises(ConflictError, match="must be signed"):
        waiver_service.notarize(_waiver(application), notary_reference="NOT-1")


def test_notarising_records_the_reference(tenant: Tenant, application) -> None:
    waiver = _waiver(application)
    waiver_service.sign(waiver, signer_name="Dana", consented=True)
    waiver_service.notarize(waiver, notary_reference="AZ-NOTARY-4417")

    assert waiver.status == WaiverStatus.NOTARIZED
    assert waiver.signature.external_reference == "AZ-NOTARY-4417"


def test_an_externally_executed_document_can_be_attached(tenant: Tenant, application) -> None:
    """A DocuSign envelope or wet ink attaches rather than being re-keyed, so
    the evidence stays with the signature."""
    waiver = _waiver(application)
    signature = waiver_service.sign(
        waiver,
        signer_name="Dana Reyes",
        consented=True,
        external_reference="docusign:envelope/8831",
    )
    assert signature.external_reference == "docusign:envelope/8831"


# ── The tie-out rules (competitive-upgrades.md U1) ──────────────────────────


def _ids(application) -> set[str]:
    return {f.rule_id for f in tieout.run(application).findings}


def test_a_waiver_matching_the_payment_reports_nothing(tenant: Tenant, application) -> None:
    _waiver(application, amount=application.line8_current_payment_due)
    assert "WAIVER-AMOUNT" not in _ids(application)


def test_a_waiver_releasing_more_than_is_paid_blocks(tenant: Tenant, application) -> None:
    """The finding that matters most: rights released for work nobody has paid
    for, discovered when the money is gone rather than when it is billed."""
    _waiver(application, amount=application.line8_current_payment_due + 50_000_00)

    report = tieout.run(application)
    assert "WAIVER-AMOUNT" in {f.rule_id for f in report.blocking}
    assert not report.ok


def test_a_waiver_releasing_less_than_is_paid_warns(tenant: Tenant, application) -> None:
    _waiver(application, amount=application.line8_current_payment_due - 1_000_00)

    report = tieout.run(application)
    assert "WAIVER-AMOUNT" in {f.rule_id for f in report.warnings}
    assert report.ok, "under-releasing is contestable, not dangerous"


def test_a_through_date_past_the_period_end_blocks(tenant: Tenant, application) -> None:
    _waiver(
        application,
        amount=application.line8_current_payment_due,
        through_date=date(2026, 8, 31),
    )

    report = tieout.run(application)
    assert "WAIVER-THROUGH-DATE" in {f.rule_id for f in report.blocking}


def test_a_through_date_before_the_period_start_warns(tenant: Tenant, application) -> None:
    _waiver(
        application,
        amount=application.line8_current_payment_due,
        through_date=date(2026, 3, 1),
    )
    assert "WAIVER-THROUGH-DATE" in _ids(application)


def test_an_edited_signed_waiver_blocks_submission(tenant: Tenant, application) -> None:
    waiver = _waiver(application, amount=application.line8_current_payment_due)
    waiver_service.sign(waiver, signer_name="Dana", consented=True)

    waiver.rendered_body += " tampered"
    db.session.flush()

    report = tieout.run(application)
    assert "WAIVER-DETACHED" in {f.rule_id for f in report.blocking}


def test_an_early_unconditional_waiver_warns(tenant: Tenant, application) -> None:
    """Signing away rights before the money arrives is the classic loss."""
    waiver = _waiver(
        application,
        amount=application.line8_current_payment_due,
        waiver_type=WaiverType.UNCONDITIONAL_PROGRESS,
    )
    waiver_service.sign(waiver, signer_name="Dana", consented=True)

    assert "WAIVER-UNCONDITIONAL-EARLY" in _ids(application)


def test_a_conditional_waiver_does_not_trigger_the_early_warning(
    tenant: Tenant, application
) -> None:
    waiver = _waiver(application, amount=application.line8_current_payment_due)
    waiver_service.sign(waiver, signer_name="Dana", consented=True)

    assert "WAIVER-UNCONDITIONAL-EARLY" not in _ids(application)


def test_an_application_with_no_waivers_reports_no_waiver_findings(
    tenant: Tenant, application
) -> None:
    assert not any(rule.startswith("WAIVER-") for rule in _ids(application))
