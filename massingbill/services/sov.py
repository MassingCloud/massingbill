"""Schedule-of-values construction and revision.

Two rules the whole billing engine leans on:

**An approved schedule is frozen.** Editing one after an application has been
built against it would silently change history; a change is a *new revision*,
and the old one becomes ``superseded`` while staying readable.

**A line's current scheduled value is derived, never typed.** It is
``base + co_adjustment``, so the original contract value of every line survives
however many change orders land on it. That is what makes G702 line 2 provable
rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    PrimeContract,
    ScheduleOfValues,
    SovLine,
    SovStatus,
    User,
)
from massingbill.models.base import utcnow
from massingbill.services import audit
from massingbill.services.money import Cents, cents


@dataclass(frozen=True)
class LineInput:
    """What a caller supplies to create or update a line."""

    item_no: str
    description: str
    scheduled_value_cents: Cents
    csi_code: str = ""
    cost_code: str = ""
    group: str = ""
    unit: str = ""
    retainage_rate_bp: int | None = None
    is_general_conditions: bool = False
    is_allowance: bool = False


def create_schedule(contract: PrimeContract, *, actor: User | None = None) -> ScheduleOfValues:
    """Start revision 1. A contract has exactly one live schedule at a time."""
    existing = db.session.scalar(
        select(func.count())
        .select_from(ScheduleOfValues)
        .where(ScheduleOfValues.prime_contract_id == contract.id)
    )
    if existing:
        raise ConflictError(
            "This contract already has a schedule of values. Create a revision instead."
        )

    schedule = ScheduleOfValues(
        organization_id=contract.organization_id,
        prime_contract_id=contract.id,
        revision=1,
        status=SovStatus.DRAFT,
    )
    db.session.add(schedule)
    db.session.flush()

    audit.record(
        contract.organization_id,
        audit.SOV_CREATED,
        entity_type="schedule_of_values",
        entity_id=schedule.id,
        after={"revision": 1, "contract_id": contract.id},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return schedule


def current_schedule(contract: PrimeContract) -> ScheduleOfValues | None:
    """The newest revision, whatever its status."""
    return db.session.scalar(
        select(ScheduleOfValues)
        .where(ScheduleOfValues.prime_contract_id == contract.id)
        .order_by(ScheduleOfValues.revision.desc())
        .limit(1)
    )


def approved_schedule(contract: PrimeContract) -> ScheduleOfValues | None:
    """The revision an application should be built against."""
    return db.session.scalar(
        select(ScheduleOfValues)
        .where(
            ScheduleOfValues.prime_contract_id == contract.id,
            ScheduleOfValues.status == SovStatus.APPROVED,
        )
        .order_by(ScheduleOfValues.revision.desc())
        .limit(1)
    )


def _require_editable(schedule: ScheduleOfValues) -> None:
    if not schedule.is_editable:
        raise ConflictError(
            f"Revision {schedule.revision} is {schedule.status} and cannot be edited. "
            "Create a new revision to make changes."
        )


def add_line(
    schedule: ScheduleOfValues, line_input: LineInput, *, actor: User | None = None
) -> SovLine:
    _require_editable(schedule)

    if not line_input.description.strip():
        raise ValidationError("A schedule-of-values line needs a description.")

    duplicate = db.session.scalar(
        select(SovLine).where(
            SovLine.schedule_id == schedule.id, SovLine.item_no == line_input.item_no
        )
    )
    if duplicate is not None:
        raise ConflictError(f"Item number {line_input.item_no!r} is already used on this schedule.")

    next_order = (
        db.session.scalar(
            select(func.coalesce(func.max(SovLine.sort_order), 0)).where(
                SovLine.schedule_id == schedule.id
            )
        )
        or 0
    ) + 10

    line = SovLine(
        organization_id=schedule.organization_id,
        schedule_id=schedule.id,
        item_no=line_input.item_no.strip(),
        description=line_input.description.strip(),
        csi_code=line_input.csi_code.strip(),
        cost_code=line_input.cost_code.strip(),
        group=line_input.group.strip(),
        unit=line_input.unit.strip(),
        sort_order=next_order,
        base_scheduled_value_cents=int(line_input.scheduled_value_cents),
        co_adjustment_cents=0,
        current_scheduled_value_cents=int(line_input.scheduled_value_cents),
        retainage_rate_bp=line_input.retainage_rate_bp,
        is_general_conditions=line_input.is_general_conditions,
        is_allowance=line_input.is_allowance,
        allowance_balance_cents=(
            int(line_input.scheduled_value_cents) if line_input.is_allowance else None
        ),
    )
    db.session.add(line)
    db.session.flush()

    audit.record(
        schedule.organization_id,
        audit.SOV_LINE_ADDED,
        entity_type="sov_line",
        entity_id=line.id,
        after=_line_snapshot(line),
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return line


def update_line(line: SovLine, line_input: LineInput, *, actor: User | None = None) -> SovLine:
    _require_editable(line.schedule)

    before = _line_snapshot(line)

    line.item_no = line_input.item_no.strip()
    line.description = line_input.description.strip()
    line.csi_code = line_input.csi_code.strip()
    line.cost_code = line_input.cost_code.strip()
    line.group = line_input.group.strip()
    line.unit = line_input.unit.strip()
    line.base_scheduled_value_cents = int(line_input.scheduled_value_cents)
    line.retainage_rate_bp = line_input.retainage_rate_bp
    line.is_general_conditions = line_input.is_general_conditions
    line.is_allowance = line_input.is_allowance

    # Derived, never typed: a change order's effect on this line is preserved.
    line.current_scheduled_value_cents = line.base_scheduled_value_cents + line.co_adjustment_cents
    db.session.flush()

    audit.record(
        line.organization_id,
        audit.SOV_LINE_UPDATED,
        entity_type="sov_line",
        entity_id=line.id,
        before=before,
        after=_line_snapshot(line),
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return line


def remove_line(line: SovLine, *, actor: User | None = None) -> None:
    _require_editable(line.schedule)

    if line.is_co_line:
        raise ConflictError(
            "This line came from a change order. Void the change order instead of "
            "deleting the line, so the contract sum stays provable."
        )

    audit.record(
        line.organization_id,
        audit.SOV_LINE_REMOVED,
        entity_type="sov_line",
        entity_id=line.id,
        before=_line_snapshot(line),
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    db.session.delete(line)
    db.session.flush()


def approve(schedule: ScheduleOfValues, *, actor: User) -> ScheduleOfValues:
    """Approve a draft, checking it ties to the contract sum.

    The tie-out engine (P4) re-checks this at every application; catching it here
    means nobody builds a month of work on a schedule that never balanced.
    """
    _require_editable(schedule)

    if not schedule.lines:
        raise ValidationError("A schedule of values needs at least one line before approval.")

    total = schedule.total_scheduled_value_cents
    expected = contract_sum_to_date(schedule)

    if total != expected:
        difference = total - expected
        raise ValidationError(
            "The schedule of values does not tie to the contract sum "
            f"(lines total {total} cents against a contract sum of {expected} cents; "
            f"difference {difference} cents).",
            details={"lines_total_cents": total, "contract_sum_cents": expected},
        )

    schedule.status = SovStatus.APPROVED
    schedule.approved_at = utcnow()
    schedule.approved_by_id = actor.id
    db.session.flush()

    audit.record(
        schedule.organization_id,
        audit.SOV_APPROVED,
        entity_type="schedule_of_values",
        entity_id=schedule.id,
        after={"revision": schedule.revision, "total_cents": total},
        actor_id=actor.id,
        actor_label=actor.email,
    )
    return schedule


def create_revision(
    schedule: ScheduleOfValues, *, note: str = "", actor: User | None = None
) -> ScheduleOfValues:
    """Copy an approved schedule into a new editable revision.

    The previous revision becomes ``superseded`` but is never modified or
    deleted -- applications already built against it must still render exactly.
    """
    if schedule.status != SovStatus.APPROVED:
        raise ConflictError("Only an approved revision can be superseded by a new one.")

    revision = ScheduleOfValues(
        organization_id=schedule.organization_id,
        prime_contract_id=schedule.prime_contract_id,
        revision=schedule.revision + 1,
        status=SovStatus.DRAFT,
        note=note,
    )
    db.session.add(revision)
    db.session.flush()

    for line in schedule.lines:
        db.session.add(
            SovLine(
                organization_id=line.organization_id,
                schedule_id=revision.id,
                item_no=line.item_no,
                description=line.description,
                csi_code=line.csi_code,
                cost_code=line.cost_code,
                group=line.group,
                unit=line.unit,
                sort_order=line.sort_order,
                base_scheduled_value_cents=line.base_scheduled_value_cents,
                co_adjustment_cents=line.co_adjustment_cents,
                current_scheduled_value_cents=line.current_scheduled_value_cents,
                quantity_milli=line.quantity_milli,
                unit_price_cents=line.unit_price_cents,
                retainage_rate_bp=line.retainage_rate_bp,
                is_co_line=line.is_co_line,
                is_general_conditions=line.is_general_conditions,
                is_allowance=line.is_allowance,
                allowance_balance_cents=line.allowance_balance_cents,
                source_change_order_id=line.source_change_order_id,
            )
        )

    schedule.status = SovStatus.SUPERSEDED
    db.session.flush()

    audit.record(
        schedule.organization_id,
        audit.SOV_REVISED,
        entity_type="schedule_of_values",
        entity_id=revision.id,
        before={"revision": schedule.revision},
        after={"revision": revision.revision, "note": note},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return revision


def get_line(schedule: ScheduleOfValues, line_id: str) -> SovLine:
    line = db.session.scalar(
        select(SovLine).where(SovLine.id == line_id, SovLine.schedule_id == schedule.id)
    )
    if line is None:
        raise NotFoundError("No such schedule-of-values line.")
    return line


def contract_sum_to_date(schedule: ScheduleOfValues) -> int:
    """G702 line 3: the original contract sum plus approved change orders.

    The schedule must tie to *this*, not to line 1. Comparing against the
    original sum would make it impossible to ever approve a revision once a
    change order had landed -- the schedule would be permanently "out of
    balance" by exactly the change order.
    """
    from massingbill.models import ChangeOrder, ChangeOrderStatus

    net_change = (
        db.session.scalar(
            select(func.coalesce(func.sum(ChangeOrder.amount_cents), 0)).where(
                ChangeOrder.prime_contract_id == schedule.prime_contract_id,
                ChangeOrder.status == ChangeOrderStatus.APPROVED,
            )
        )
        or 0
    )
    return schedule.prime_contract.original_contract_sum_cents + int(net_change)


def reconciliation(schedule: ScheduleOfValues) -> dict[str, int]:
    """How far the schedule is from the contract sum to date, in cents."""
    total = schedule.total_scheduled_value_cents
    expected = contract_sum_to_date(schedule)
    return {
        "lines_total_cents": total,
        "contract_sum_cents": expected,
        "difference_cents": total - expected,
    }


def _line_snapshot(line: SovLine) -> dict[str, object]:
    return {
        "item_no": line.item_no,
        "description": line.description,
        "csi_code": line.csi_code,
        "scheduled_value_cents": line.current_scheduled_value_cents,
        "retainage_rate_bp": line.retainage_rate_bp,
    }


__all__ = [
    "LineInput",
    "add_line",
    "approve",
    "approved_schedule",
    "cents",
    "create_revision",
    "create_schedule",
    "current_schedule",
    "get_line",
    "reconciliation",
    "remove_line",
    "update_line",
]
