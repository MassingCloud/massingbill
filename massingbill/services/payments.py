"""Recording payments received, and reconciling them against what was certified.

We do not move money. We record what arrived, which is what makes three other
things possible: the payment-variance check, retainage-release forecasting, and
being able to show that a conditional waiver actually took effect.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import Application, ApplicationStatus, Payment, PaymentMethod, User
from massingbill.services import audit, events
from massingbill.services.money import Cents, cents


def record(
    application: Application,
    *,
    amount: Cents,
    received_on: date,
    method: PaymentMethod = PaymentMethod.CHECK,
    reference: str = "",
    joint_payee: str = "",
    note: str = "",
    actor: User | None = None,
) -> Payment:
    """Record money received against one application."""
    if application.is_editable:
        raise ValidationError(
            "This application has not been submitted, so there is nothing to pay against."
        )
    if int(amount) == 0:
        raise ValidationError("A recorded payment needs an amount.")

    payment = Payment(
        organization_id=application.organization_id,
        application_id=application.id,
        amount_cents=int(amount),
        received_on=received_on,
        method=method,
        reference=reference.strip(),
        joint_payee=joint_payee.strip(),
        note=note.strip(),
    )
    db.session.add(payment)

    # Fully paid closes the period. Partly paid leaves it certified, because a
    # conditional waiver has not taken effect on a part payment.
    if paid_to_date(application) + int(amount) >= application.certified_payment_cents:
        application.status = ApplicationStatus.PAID

    db.session.flush()

    audit.record(
        application.organization_id,
        audit.PAYMENT_RECORDED,
        entity_type="payment",
        entity_id=payment.id,
        after={
            "amount_cents": payment.amount_cents,
            "received_on": received_on.isoformat(),
            "method": str(method),
        },
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    events.application_paid(application, payment)
    return payment


def paid_to_date(application: Application) -> int:
    total = db.session.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
            Payment.application_id == application.id
        )
    )
    return int(total or 0)


def variance(application: Application) -> Cents:
    """What was certified, less what actually arrived.

    Positive means money is still outstanding.
    """
    return cents(application.certified_payment_cents - paid_to_date(application))


def payments_for(application: Application) -> list[Payment]:
    return list(
        db.session.scalars(
            select(Payment)
            .where(Payment.application_id == application.id)
            .order_by(Payment.received_on)
        )
    )
