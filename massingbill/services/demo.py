"""The demo project.

A realistic twelve-line, six-period job that exercises everything the engine
does: a change order that adds a line, stored material that later installs, a
deductive change order, a stepped retainage reduction, an architect certifying
less than was asked for, waivers, compliance documents, subcontracts and a
recorded payment.

Deterministic and offline. Every date is fixed and every amount is chosen so a
reader can check the arithmetic, because the point of a demo for *this* product
is not that it looks plausible -- it is that the numbers are verifiable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from massingbill.errors import ConflictError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    ComplianceKind,
    ContractParty,
    Organization,
    PartyRole,
    PaymentMethod,
    PrimeContract,
    Project,
    ProjectStatus,
    RetainageMode,
    RetainageRule,
    Role,
    ScheduleOfValues,
    StorageLocation,
    StoredMaterial,
    User,
    WaiverType,
)
from massingbill.services import accounts, seeding
from massingbill.services import application as app_service
from massingbill.services import change_order as co_service
from massingbill.services import compliance as compliance_service
from massingbill.services import deadlines as deadline_service
from massingbill.services import payments as payment_service
from massingbill.services import sov as sov_service
from massingbill.services import subcontracts as sub_service
from massingbill.services import waivers as waiver_service
from massingbill.services.money import Cents, cents

DEMO_EMAIL = "demo@massingbill.example"
DEMO_PASSWORD = "demo-account-not-for-production"
CONTRACT_SUM = 4_850_000_00

#: Twelve lines across nine CSI divisions. Rounded so the totals are checkable.
LINES: list[tuple[str, str, str, int]] = [
    ("001", "01", "General conditions", 340_000_00),
    ("002", "02", "Demolition and site clearing", 185_000_00),
    ("003", "03", "Concrete — foundations and slabs", 720_000_00),
    ("004", "04", "Masonry", 265_000_00),
    ("005", "05", "Structural steel", 890_000_00),
    ("006", "06", "Rough and finish carpentry", 210_000_00),
    ("007", "07", "Roofing and waterproofing", 305_000_00),
    ("008", "08", "Glazing and curtain wall", 615_000_00),
    ("009", "09", "Interior finishes", 430_000_00),
    ("010", "22", "Plumbing", 275_000_00),
    ("011", "23", "HVAC", 395_000_00),
    ("012", "26", "Electrical", 220_000_00),
]


@dataclass
class Demo:
    organization: Organization
    user: User
    project: Project
    contract: PrimeContract
    #: Non-empty when the statutory waiver form refused to render, which in
    #: California it should. Surfaced by the demo site.
    waiver_refusal: str = ""


def build(*, email: str = DEMO_EMAIL, password: str = DEMO_PASSWORD) -> Demo:
    """Create the demo tenant and run six periods against it."""
    user = accounts.create_user(email, password, name="Dana Reyes")
    organization = accounts.create_organization("Northgate Builders", user)
    seeding.seed_cost_codes(organization)
    waiver_service.seed_templates(organization)
    deadline_service.seed_rules(organization)

    owner = ContractParty(
        organization_id=organization.id,
        name="Riverside Health Partners LLC",
        party_role=PartyRole.OWNER,
        contact_name="M. Okonjo",
        address="200 Capitol Mall, Suite 1400, Sacramento, CA 95814",
    )
    architect = ContractParty(
        organization_id=organization.id,
        name="Ferris & Partners Architects",
        party_role=PartyRole.ARCHITECT,
        contact_name="J. Ferris, AIA",
        address="55 Front Street, Sacramento, CA 95814",
    )
    db.session.add_all([owner, architect])
    db.session.flush()

    project = Project(
        organization_id=organization.id,
        number="2026-014",
        name="Riverside Medical Office Building",
        address="1400 Riverside Drive, Sacramento, CA 95811",
        jurisdiction_state="CA",
        status=ProjectStatus.ACTIVE,
        owner_party_id=owner.id,
        architect_party_id=architect.id,
    )
    db.session.add(project)
    db.session.flush()

    # California: SB 61 caps private-works retention at 5% from 2026-01-01, so
    # the demo starts there rather than at the more common 10%.
    rule = RetainageRule(
        organization_id=organization.id,
        mode=RetainageMode.SPLIT,
        rate_work_bp=500,
        rate_stored_bp=250,
        statutory_cap_bp=500,
        statute_citation="Cal. Civ. Code § 8812 (SB 61, eff. 2026-01-01)",
        cap_enforcement="warn",
    )
    db.session.add(rule)
    db.session.flush()

    contract = PrimeContract(
        organization_id=organization.id,
        project_id=project.id,
        number="PC-2026-014",
        original_contract_sum_cents=CONTRACT_SUM,
        execution_date=date(2026, 1, 12),
        retainage_rule_id=rule.id,
        stored_materials_allowed=True,
        offsite_stored_allowed=True,
    )
    db.session.add(contract)
    db.session.flush()

    schedule = sov_service.create_schedule(contract, actor=user)
    for item, csi, description, value in LINES:
        sov_service.add_line(
            schedule,
            sov_service.LineInput(
                item_no=item,
                description=description,
                scheduled_value_cents=cents(value),
                csi_code=csi,
            ),
            actor=user,
        )
    sov_service.approve(schedule, actor=user)

    _compliance(project, user)
    _subcontracts(project, user)
    db.session.commit()

    refusal = _run_periods(contract, user)
    db.session.commit()

    return Demo(
        organization=organization,
        user=user,
        project=project,
        contract=contract,
        waiver_refusal=refusal,
    )


def _compliance(project: Project, user: User) -> None:
    compliance_service.add_requirement(
        project,
        ComplianceKind.CERTIFICATE_OF_INSURANCE,
        blocks_payment=True,
        description="General liability, $2M per occurrence",
        actor=user,
    )
    coi = compliance_service.requirements_for(project)[0]
    compliance_service.file_document(
        coi,
        filename="northgate-coi-2026.pdf",
        effective_from=date(2026, 1, 1),
        expires_on=date(2026, 12, 31),
        actor=user,
    )
    compliance_service.add_requirement(project, ComplianceKind.W9, blocks_payment=False, actor=user)


def _subcontracts(project: Project, user: User) -> None:
    for number, vendor, amount, csi in (
        ("SC-001", "Delta Mechanical", 395_000_00, "23"),
        ("SC-002", "Meridian Electric", 220_000_00, "26"),
        ("SC-003", "Cascade Glazing", 615_000_00, "08"),
    ):
        subcontract = sub_service.create(
            project,
            number=number,
            vendor_name=vendor,
            amount=cents(amount),
            csi_code=csi,
            retainage_rate_bp=500,
            contact_email=f"ap@{vendor.split()[0].lower()}.example",
            actor=user,
        )
        sub_service.execute(subcontract, on=date(2026, 1, 20))


def _run_periods(contract: PrimeContract, user: User) -> str:
    """Six months of billing, each one demonstrating something different."""
    # Period 1 — first billing.
    _period(
        contract,
        user,
        month=2,
        work={"001": 42_000_00, "002": 185_000_00, "003": 120_000_00},
    )

    # Period 2 — steady progress.
    _period(
        contract,
        user,
        month=3,
        work={"001": 42_000_00, "003": 340_000_00, "005": 180_000_00},
    )

    # Period 3 — a change order adds a line for a rooftop screen.
    schedule = _schedule(contract)
    revision = sov_service.create_revision(schedule, actor=user)
    order = co_service.create(
        contract, number="CO-001", description="Rooftop mechanical screen", actor=user
    )
    co_service.add_line(
        order,
        amount=cents(148_000_00),
        new_item_no="013",
        description="Rooftop mechanical screen",
        csi_code="05",
    )
    co_service.approve(order, revision, approved_date=date(2026, 4, 14), actor=user)
    sov_service.approve(revision, actor=user)
    db.session.commit()

    _period(
        contract,
        user,
        month=4,
        work={"001": 42_000_00, "003": 260_000_00, "005": 340_000_00, "004": 90_000_00},
    )

    # Period 4 — curtain-wall units arrive and sit in column F.
    schedule = _schedule(contract)
    glazing = next(line for line in schedule.lines if line.item_no == "008")
    curtain_wall = StoredMaterial(
        organization_id=contract.organization_id,
        sov_line_id=glazing.id,
        description="Curtain wall units, 42 bays",
        location=StorageLocation.BONDED_OFFSITE,
        value_cents=245_000_00,
        supplier="Cascade Glazing",
        invoice_ref="INV-2026-4471",
        bond_ref="BOND-CG-889",
        insurance_ref="COI-CG-2026",
        stored_on=date(2026, 5, 8),
    )
    db.session.add(curtain_wall)
    db.session.flush()

    application = _period(
        contract,
        user,
        month=5,
        work={"001": 42_000_00, "005": 370_000_00, "004": 175_000_00, "007": 120_000_00},
        submit=False,
    )
    app_service.apply_stored_materials(application)
    _finish(application, user, certify=None)

    # Period 5 — the curtain wall installs: column F empties into column E.
    application = app_service.open_period(
        contract,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        actor=user,
    )
    app_service.install_material(curtain_wall, application)
    _enter(
        application,
        {"001": 42_000_00, "007": 185_000_00, "008": 245_000_00, "010": 95_000_00},
    )
    app_service.apply_stored_materials(application)
    _finish(application, user, certify=None)

    # Period 6 — a deductive change order, and the architect certifies less.
    schedule = _schedule(contract)
    revision = sov_service.create_revision(schedule, actor=user)
    finishes = next(line for line in revision.lines if line.item_no == "009")
    deduction = co_service.create(
        contract, number="CO-002", description="Interior finishes scope reduced", actor=user
    )
    co_service.add_line(deduction, amount=cents(-62_000_00), sov_line=finishes)
    co_service.approve(deduction, revision, approved_date=date(2026, 7, 9), actor=user)
    sov_service.approve(revision, actor=user)
    db.session.commit()

    application = _period(
        contract,
        user,
        month=7,
        work={"001": 42_000_00, "006": 130_000_00, "011": 210_000_00, "012": 95_000_00},
        submit=False,
    )
    # The architect declines $18,000 of the carpentry line.
    _finish(application, user, certify=cents(application.line8_current_payment_due - 18_000_00))

    return _waivers_and_payment(contract, user)


def _schedule(contract: PrimeContract) -> ScheduleOfValues:
    """The approved schedule, which the demo always has by this point.

    ``approved_schedule`` is legitimately optional -- a contract may not have one
    yet -- but a demo that reached here without one is a bug in the demo, and
    should say so rather than fail three calls later on ``None.lines``.
    """
    schedule = sov_service.approved_schedule(contract)
    if schedule is None:  # pragma: no cover - a broken demo, not a user path
        raise RuntimeError("the demo contract has no approved schedule of values")
    return schedule


def _period(
    contract: PrimeContract,
    user: User,
    *,
    month: int,
    work: dict[str, int],
    submit: bool = True,
) -> Application:
    application = app_service.open_period(
        contract,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        actor=user,
    )
    _enter(application, work)
    if submit:
        _finish(application, user, certify=None)
    return application


def _enter(application: Application, work: dict[str, int]) -> None:
    """Enter this period's work, added to whatever each line already carries."""
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(
                line_id=line.id,
                this_period=cents(work.get(line.item_no, 0)),
                stored=cents(line.col_f_stored),
            )
            for line in application.lines
        ],
    )


def _finish(application: Application, user: User, *, certify: Cents | None) -> None:
    app_service.submit(application, actor=user)
    if certify is not None:
        app_service.certify(
            application,
            certify,
            certified_by_label="Ferris & Partners Architects",
            reason="Carpentry line not accepted as complete",
            actor=user,
        )
    db.session.commit()


def _waivers_and_payment(contract: PrimeContract, user: User) -> str:
    """Attempt a waiver, and record the payment for the period before.

    The project is in California, which prescribes its waiver wording -- so the
    attempt is *expected to be refused*, and the refusal is returned so the demo
    site can show it. That is the feature, not an obstacle to route around: this
    build ships the statutory forms empty, and the engine will not issue one
    until somebody has read the statute and entered the text.
    """
    applications = app_service.applications_for(contract)
    latest = applications[-1]
    refusal = ""

    try:
        waiver = waiver_service.request(
            latest,
            waiver_type=WaiverType.CONDITIONAL_PROGRESS,
            claimant="Northgate Builders",
            customer="Riverside Health Partners LLC",
            amount=cents(latest.certified_payment_cents),
            actor=user,
        )
    except ConflictError as exc:
        refusal = str(exc)
        db.session.rollback()
    else:
        waiver_service.sign(
            waiver,
            signer_name="Dana Reyes",
            signer_title="Controller",
            signer_email=user.email,
            consented=True,
            ip="203.0.113.24",
            user_agent="Massing Bill demo",
            signer=user,
        )

    # The period before the latest was paid in full.
    applications = app_service.applications_for(contract)
    paid = applications[-2]
    payment_service.record(
        paid,
        amount=cents(paid.certified_payment_cents),
        received_on=date(2026, 7, 22),
        method=PaymentMethod.ACH,
        reference="ACH-2026-07-22-118",
        actor=user,
    )
    db.session.commit()
    return refusal


def owner_role() -> Role:
    return Role.OWNER
