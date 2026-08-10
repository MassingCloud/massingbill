"""Change orders and their effect on the schedule of values.

Approving a change order does two things at once, in one transaction: it moves
the change order to ``approved``, and it lands its lines on the schedule as
**adjustments**. It never overwrites a line's base value, so
``base + co_adjustment`` always reconstructs how a line reached its current
scheduled value.

That is what makes G702 line 2 provable: the sum of approved change orders and
the sum of the adjustments on the schedule are the same number, computed two
different ways, and the tie-out engine checks they agree.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from massingbill.errors import ConflictError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    ChangeOrder,
    ChangeOrderLine,
    ChangeOrderStatus,
    ChangeOrderType,
    PrimeContract,
    ScheduleOfValues,
    SovLine,
    User,
)
from massingbill.services import audit, events
from massingbill.services.money import Cents, cents


def create(
    contract: PrimeContract,
    *,
    number: str,
    description: str = "",
    co_type: ChangeOrderType = ChangeOrderType.OWNER_CO,
    actor: User | None = None,
) -> ChangeOrder:
    duplicate = db.session.scalar(
        select(ChangeOrder).where(
            ChangeOrder.prime_contract_id == contract.id, ChangeOrder.number == number
        )
    )
    if duplicate is not None:
        raise ConflictError(f"Change order {number!r} already exists on this contract.")

    order = ChangeOrder(
        organization_id=contract.organization_id,
        prime_contract_id=contract.id,
        number=number.strip(),
        description=description.strip(),
        co_type=co_type,
        status=ChangeOrderStatus.DRAFT,
        amount_cents=0,
    )
    db.session.add(order)
    db.session.flush()

    audit.record(
        contract.organization_id,
        audit.CO_CREATED,
        entity_type="change_order",
        entity_id=order.id,
        after={"number": order.number, "type": str(co_type)},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return order


def add_line(
    order: ChangeOrder,
    *,
    amount: Cents,
    sov_line: SovLine | None = None,
    new_item_no: str = "",
    description: str = "",
    csi_code: str = "",
) -> ChangeOrderLine:
    """Adjust an existing schedule line, or create a new one."""
    _require_draft(order)

    if sov_line is None and not new_item_no.strip():
        raise ValidationError(
            "A change-order line must either adjust an existing schedule line or "
            "name a new item number."
        )

    line = ChangeOrderLine(
        organization_id=order.organization_id,
        change_order_id=order.id,
        sov_line_id=sov_line.id if sov_line is not None else None,
        new_item_no=new_item_no.strip(),
        description=description.strip(),
        csi_code=csi_code.strip(),
        amount_cents=int(amount),
    )
    db.session.add(line)
    db.session.flush()

    order.amount_cents = _line_total(order)
    db.session.flush()
    return line


def _line_total(order: ChangeOrder) -> int:
    return (
        db.session.scalar(
            select(func.coalesce(func.sum(ChangeOrderLine.amount_cents), 0)).where(
                ChangeOrderLine.change_order_id == order.id
            )
        )
        or 0
    )


def approve(
    order: ChangeOrder,
    schedule: ScheduleOfValues,
    *,
    approved_date: date | None = None,
    applies_to: Application | None = None,
    actor: User | None = None,
) -> ChangeOrder:
    """Approve the change order and land it on the schedule of values.

    The schedule must be editable. Approving into a frozen revision would
    restate a period that has already been billed.
    """
    _require_draft(order)

    if not order.lines:
        raise ValidationError("A change order needs at least one line before approval.")

    if not schedule.is_editable:
        raise ConflictError(
            f"Schedule revision {schedule.revision} is {schedule.status}. "
            "Create a new revision before approving a change order into it."
        )

    next_order = (
        db.session.scalar(
            select(func.coalesce(func.max(SovLine.sort_order), 0)).where(
                SovLine.schedule_id == schedule.id
            )
        )
        or 0
    )

    for co_line in order.lines:
        if co_line.creates_line:
            next_order += 10
            db.session.add(
                SovLine(
                    organization_id=order.organization_id,
                    schedule_id=schedule.id,
                    item_no=co_line.new_item_no,
                    description=co_line.description or order.description,
                    csi_code=co_line.csi_code,
                    sort_order=next_order,
                    base_scheduled_value_cents=0,
                    co_adjustment_cents=co_line.amount_cents,
                    current_scheduled_value_cents=co_line.amount_cents,
                    is_co_line=True,
                    source_change_order_id=order.id,
                )
            )
        else:
            sov_line = db.session.get(SovLine, co_line.sov_line_id)
            if sov_line is None:
                raise ValidationError(
                    "Change-order line refers to a schedule line that no longer exists."
                )
            # An adjustment, not an overwrite: the original contract value of
            # this line stays recoverable.
            sov_line.co_adjustment_cents += co_line.amount_cents
            sov_line.current_scheduled_value_cents = (
                sov_line.base_scheduled_value_cents + sov_line.co_adjustment_cents
            )

    order.status = ChangeOrderStatus.APPROVED
    order.approved_date = approved_date or date.today()
    order.applies_to_application_id = applies_to.id if applies_to is not None else None
    db.session.flush()

    audit.record(
        order.organization_id,
        audit.CO_APPROVED,
        entity_type="change_order",
        entity_id=order.id,
        after={"number": order.number, "amount_cents": order.amount_cents},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    events.change_order_approved(order)
    return order


def void(order: ChangeOrder, schedule: ScheduleOfValues, *, actor: User | None = None) -> None:
    """Reverse an approved change order, backing its adjustments out.

    Voiding rather than deleting, and reversing the adjustment rather than
    resetting it, so a line that carried two change orders keeps the other one.
    """
    if order.status != ChangeOrderStatus.APPROVED:
        order.status = ChangeOrderStatus.VOID
        db.session.flush()
        return

    if not schedule.is_editable:
        raise ConflictError(
            "The schedule revision this change order landed on is frozen. "
            "Issue a reversing change order instead."
        )

    for co_line in order.lines:
        if co_line.creates_line:
            created = db.session.scalar(
                select(SovLine).where(
                    SovLine.schedule_id == schedule.id,
                    SovLine.source_change_order_id == order.id,
                    SovLine.item_no == co_line.new_item_no,
                )
            )
            if created is not None:
                db.session.delete(created)
        else:
            sov_line = db.session.get(SovLine, co_line.sov_line_id)
            if sov_line is not None:
                sov_line.co_adjustment_cents -= co_line.amount_cents
                sov_line.current_scheduled_value_cents = (
                    sov_line.base_scheduled_value_cents + sov_line.co_adjustment_cents
                )

    order.status = ChangeOrderStatus.VOID
    db.session.flush()

    audit.record(
        order.organization_id,
        audit.CO_VOIDED,
        entity_type="change_order",
        entity_id=order.id,
        before={"amount_cents": order.amount_cents},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


def approved_total(contract: PrimeContract) -> Cents:
    """G702 line 2, computed from the change orders themselves."""
    total = (
        db.session.scalar(
            select(func.coalesce(func.sum(ChangeOrder.amount_cents), 0)).where(
                ChangeOrder.prime_contract_id == contract.id,
                ChangeOrder.status == ChangeOrderStatus.APPROVED,
            )
        )
        or 0
    )
    return cents(int(total))


def adjustment_total(schedule: ScheduleOfValues) -> Cents:
    """The same number, computed from the schedule of values instead.

    Two independent routes to line 2. The tie-out engine asserts they agree,
    which is what makes the figure provable rather than asserted.
    """
    return cents(sum(line.co_adjustment_cents for line in schedule.lines))


def for_contract(contract: PrimeContract) -> list[ChangeOrder]:
    return list(
        db.session.scalars(
            select(ChangeOrder)
            .where(ChangeOrder.prime_contract_id == contract.id)
            .order_by(ChangeOrder.number)
        )
    )


def _require_draft(order: ChangeOrder) -> None:
    if order.status != ChangeOrderStatus.DRAFT:
        raise ConflictError(
            f"Change order {order.number} is {order.status} and can no longer be edited."
        )
