"""Subcontracts and the billings received against them.

The AP mirror of the prime side, reusing the same money kernel so the two halves
cannot drift. A sub billing is *received* and *approved*, not submitted and
certified -- deliberately different words, because an audit log that used the
same ones would be ambiguous about which side of the contract an event was on.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    Project,
    SubApplication,
    SubApplicationStatus,
    Subcontract,
    SubcontractLine,
    SubcontractStatus,
    User,
)
from massingbill.models.base import utcnow
from massingbill.services import audit
from massingbill.services.money import Bp, Cents, apply_bp, bp, cents


def create(
    project: Project,
    *,
    number: str,
    vendor_name: str,
    amount: Cents,
    scope: str = "",
    csi_code: str = "",
    retainage_rate_bp: int = 1000,
    contact_name: str = "",
    contact_email: str = "",
    actor: User | None = None,
) -> Subcontract:
    duplicate = db.session.scalar(
        select(Subcontract).where(
            Subcontract.project_id == project.id, Subcontract.number == number
        )
    )
    if duplicate is not None:
        raise ConflictError(f"Subcontract {number!r} already exists on this project.")
    if int(amount) <= 0:
        raise ValidationError("A subcontract needs an amount greater than zero.")

    subcontract = Subcontract(
        organization_id=project.organization_id,
        project_id=project.id,
        number=number.strip(),
        vendor_name=vendor_name.strip(),
        scope=scope.strip(),
        csi_code=csi_code.strip(),
        original_amount_cents=int(amount),
        retainage_rate_bp=retainage_rate_bp,
        contact_name=contact_name.strip(),
        contact_email=contact_email.strip().lower(),
        status=SubcontractStatus.DRAFT,
    )
    db.session.add(subcontract)
    db.session.flush()

    audit.record(
        project.organization_id,
        audit.SUBCONTRACT_CREATED,
        entity_type="subcontract",
        entity_id=subcontract.id,
        after={"number": subcontract.number, "vendor": subcontract.vendor_name},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return subcontract


def execute(subcontract: Subcontract, *, on: date | None = None) -> Subcontract:
    subcontract.status = SubcontractStatus.EXECUTED
    subcontract.executed_on = on or date.today()
    db.session.flush()
    return subcontract


def add_line(
    subcontract: Subcontract,
    *,
    item_no: str,
    description: str,
    amount: Cents,
    sov_line_id: str | None = None,
) -> SubcontractLine:
    """Add a line to the sub's own schedule of values."""
    next_order = (
        db.session.scalar(
            select(func.coalesce(func.max(SubcontractLine.sort_order), 0)).where(
                SubcontractLine.subcontract_id == subcontract.id
            )
        )
        or 0
    ) + 10

    line = SubcontractLine(
        organization_id=subcontract.organization_id,
        subcontract_id=subcontract.id,
        sov_line_id=sov_line_id,
        item_no=item_no.strip(),
        description=description.strip(),
        sort_order=next_order,
        scheduled_value_cents=int(amount),
    )
    db.session.add(line)
    db.session.flush()
    return line


# ── Billings ────────────────────────────────────────────────────────────────


def next_number(subcontract: Subcontract) -> int:
    highest = db.session.scalar(
        select(func.max(SubApplication.number)).where(
            SubApplication.subcontract_id == subcontract.id
        )
    )
    return (highest or 0) + 1


def previous_paid(subcontract: Subcontract, before_number: int) -> Cents:
    """Everything approved on this subcontract before the given billing.

    Uses the *approved* amount rather than the billed one, for the same reason
    the prime side's line 7 follows the certificate: an amount the GC declined
    to approve has not been paid, and must not be treated as if it had.
    """
    rows = db.session.scalars(
        select(SubApplication).where(
            SubApplication.subcontract_id == subcontract.id,
            SubApplication.number < before_number,
            SubApplication.status.in_((SubApplicationStatus.APPROVED, SubApplicationStatus.PAID)),
        )
    )
    return cents(sum(row.approved_or_billed_cents for row in rows))


def receive(
    subcontract: Subcontract,
    *,
    period_start: date,
    period_end: date,
    completed_to_date: Cents,
    stored: Cents = cents(0),
    is_final: bool = False,
    application: Application | None = None,
    actor: User | None = None,
) -> SubApplication:
    """Record a billing received from a subcontractor.

    Retainage is computed here rather than accepted from the sub: what the
    subcontract says is withheld is what gets withheld, whatever arrived on the
    paperwork.
    """
    if subcontract.status not in (SubcontractStatus.EXECUTED, SubcontractStatus.DRAFT):
        raise ConflictError(
            f"Subcontract {subcontract.number} is {subcontract.status} and cannot be billed."
        )
    if period_end < period_start:
        raise ValidationError("The period end cannot precede the period start.")

    total = cents(int(completed_to_date) + int(stored))
    if total > subcontract.current_amount_cents:
        raise ValidationError(
            f"This billing takes the subcontract to {total} cents against a value of "
            f"{subcontract.current_amount_cents} cents."
        )

    number = next_number(subcontract)
    rate: Bp = bp(0) if is_final else bp(subcontract.retainage_rate_bp)
    retainage = apply_bp(total, rate)
    previous = previous_paid(subcontract, number)

    billing = SubApplication(
        organization_id=subcontract.organization_id,
        subcontract_id=subcontract.id,
        application_id=application.id if application is not None else None,
        number=number,
        period_start=period_start,
        period_end=period_end,
        status=SubApplicationStatus.RECEIVED,
        completed_to_date_cents=int(completed_to_date),
        stored_cents=int(stored),
        retainage_cents=int(retainage),
        previous_payments_cents=int(previous),
        payment_due_cents=int(total) - int(retainage) - int(previous),
        is_final=is_final,
        received_at=utcnow(),
    )
    db.session.add(billing)
    db.session.flush()

    audit.record(
        subcontract.organization_id,
        audit.SUB_BILLING_RECEIVED,
        entity_type="sub_application",
        entity_id=billing.id,
        after={
            "subcontract": subcontract.number,
            "number": number,
            "due_cents": billing.payment_due_cents,
        },
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return billing


def approve(
    billing: SubApplication,
    *,
    amount: Cents | None = None,
    note: str = "",
    actor: User | None = None,
) -> SubApplication:
    """Approve a sub billing, in full or in part."""
    if billing.status != SubApplicationStatus.RECEIVED:
        raise ConflictError(f"This billing is {billing.status} and cannot be approved.")

    approved = billing.payment_due_cents if amount is None else int(amount)
    if approved > billing.payment_due_cents:
        raise ValidationError(
            "A billing cannot be approved for more than it claims. Ask for a revised "
            "billing instead."
        )

    billing.approved_amount_cents = approved
    billing.approval_note = note
    billing.status = SubApplicationStatus.APPROVED
    billing.approved_at = utcnow()
    db.session.flush()

    audit.record(
        billing.organization_id,
        audit.SUB_BILLING_APPROVED,
        entity_type="sub_application",
        entity_id=billing.id,
        after={"approved_cents": approved, "claimed_cents": billing.payment_due_cents},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return billing


def reject(billing: SubApplication, *, reason: str, actor: User | None = None) -> None:
    if billing.status != SubApplicationStatus.RECEIVED:
        raise ConflictError(f"This billing is {billing.status} and cannot be rejected.")

    billing.status = SubApplicationStatus.REJECTED
    billing.approval_note = reason
    db.session.flush()

    audit.record(
        billing.organization_id,
        audit.SUB_BILLING_REJECTED,
        entity_type="sub_application",
        entity_id=billing.id,
        after={"reason": reason},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


def for_project(project: Project) -> list[Subcontract]:
    return list(
        db.session.scalars(
            select(Subcontract)
            .where(Subcontract.project_id == project.id)
            .order_by(Subcontract.number)
        )
    )


def get_billing(subcontract: Subcontract, billing_id: str) -> SubApplication:
    billing = db.session.scalar(
        select(SubApplication).where(
            SubApplication.id == billing_id,
            SubApplication.subcontract_id == subcontract.id,
        )
    )
    if billing is None:
        raise NotFoundError("No such billing on this subcontract.")
    return billing


def committed_total(project: Project) -> Cents:
    """Everything committed to subcontractors on this project."""
    return cents(sum(sub.current_amount_cents for sub in for_project(project)))


def approved_to_date(subcontract: Subcontract) -> Cents:
    return cents(
        sum(
            billing.approved_or_billed_cents
            for billing in subcontract.applications
            if billing.status in (SubApplicationStatus.APPROVED, SubApplicationStatus.PAID)
        )
    )
