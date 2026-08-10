"""Domain events, and the payloads they carry to subscribers.

One module so that every event payload is defined in exactly one place -- the
same place the OpenAPI document describes and the SDK types. When payload
shapes are built inline at each call site they diverge, and a subscriber
discovers it in production.

Payloads are deliberately **thin**: identifiers, the number a human would quote,
and the amounts. A subscriber that needs the full application asks the API for
it. Fat event payloads become a second, undocumented API that nobody versions.

Emitting is free when nobody is listening: with no subscriptions,
:func:`~massingbill.services.webhooks.emit` writes nothing and returns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from massingbill.models import WebhookEvent
from massingbill.services import webhooks

if TYPE_CHECKING:
    from massingbill.models import Application, ChangeOrder, Payment, WaiverInstance
    from massingbill.services.tieout import TieoutReport


def _application_ref(application: Application) -> dict[str, Any]:
    """The identity of an application, as every payload refers to it."""
    return {
        "application_id": application.id,
        "number": application.number,
        "prime_contract_id": application.prime_contract_id,
        "period_start": application.period_start.isoformat(),
        "period_end": application.period_end.isoformat(),
    }


def application_submitted(application: Application) -> None:
    webhooks.emit(
        application.organization_id,
        WebhookEvent.APPLICATION_SUBMITTED,
        _application_ref(application)
        | {
            "completed_and_stored_cents": application.line4_completed_stored,
            "retainage_cents": application.line5_total_retainage,
            "payment_due_cents": application.line8_current_payment_due,
        },
    )


def application_certified(application: Application, certification: Any) -> None:
    webhooks.emit(
        application.organization_id,
        WebhookEvent.APPLICATION_CERTIFIED,
        _application_ref(application)
        | {
            "certified_cents": certification.amount_certified_cents,
            # Negative when the architect certified less than was applied for,
            # which is the number a subscriber actually wants to alert on.
            "variance_cents": certification.variance_cents,
            "certified_by": certification.certified_by_label,
        },
    )


def application_paid(application: Application, payment: Payment) -> None:
    webhooks.emit(
        application.organization_id,
        WebhookEvent.APPLICATION_PAID,
        _application_ref(application)
        | {
            "payment_id": payment.id,
            "amount_cents": payment.amount_cents,
            "received_on": payment.received_on.isoformat(),
            "method": str(payment.method),
        },
    )


def change_order_approved(order: ChangeOrder) -> None:
    webhooks.emit(
        order.organization_id,
        WebhookEvent.CO_APPROVED,
        {
            "change_order_id": order.id,
            "number": order.number,
            "description": order.description,
            "amount_cents": order.amount_cents,
            "approved_date": order.approved_date.isoformat() if order.approved_date else None,
        },
    )


def waiver_signed(waiver: WaiverInstance) -> None:
    webhooks.emit(
        waiver.organization_id,
        WebhookEvent.WAIVER_SIGNED,
        {
            "waiver_id": waiver.id,
            "waiver_type": str(waiver.waiver_type),
            "through_date": waiver.through_date.isoformat() if waiver.through_date else None,
            "amount_cents": waiver.amount_cents,
        },
    )


def tieout_failed(application: Application, report: TieoutReport) -> None:
    webhooks.emit(
        application.organization_id,
        WebhookEvent.TIEOUT_FAILED,
        _application_ref(application)
        | {
            "findings": [
                {"rule_id": f.rule_id, "severity": str(f.severity), "message": f.message}
                for f in report.blocking
            ]
        },
    )


def sweep_open_periods(organization_id: str) -> list[tuple[Application, TieoutReport]]:
    """Check every editable application and announce the ones that will not fly.

    The point of running this on a schedule: a project accountant finds out
    which draft does not balance *days* before the deadline, rather than at the
    moment they try to submit it. Returns only the failures, because a list of
    everything that is fine is a list nobody reads.
    """
    from massingbill.extensions import db
    from massingbill.models import EDITABLE_STATUSES
    from massingbill.models import Application as App

    open_applications = db.session.scalars(
        db.select(App).where(
            App.organization_id == organization_id,
            App.status.in_(EDITABLE_STATUSES),
        )
    )

    failures = []
    for application in open_applications:
        report = check_and_announce(application)
        if report.blocking:
            failures.append((application, report))
    return failures


def check_and_announce(application: Application) -> TieoutReport:
    """Run the tie-out and announce a failure to subscribers.

    Separate from ``application.submit`` deliberately. Submit *refuses* and
    raises, so anything queued there dies in the caller's rollback -- the same
    reason the audit chain flushes into the caller's transaction rather than
    committing on its own. This runs as its own unit of work, so the event
    survives.

    Called from :func:`sweep_open_periods`, not from ``GET .../tieout`` -- a
    read should not queue a webhook, or polling the endpoint would spam every
    subscriber.
    """
    from massingbill.extensions import db
    from massingbill.services import tieout

    report = tieout.run(application)
    if report.blocking:
        tieout_failed(application, report)
        db.session.commit()
    return report
