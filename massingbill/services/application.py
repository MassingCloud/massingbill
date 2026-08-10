"""The period engine: opening, computing and closing a monthly requisition.

The shape of a period:

1. **Open.** Seed one application line per schedule-of-values line. Column D and
   G702 line 7 are read from the preceding application and written *once*. They
   are never editable and never recomputed -- both describe money already
   billed, and a system that lets someone retype them will eventually let
   someone bill it twice.
2. **Enter.** Columns E (work this period) and F (stored material) are the only
   inputs. Everything else is derived.
3. **Recompute.** G = D+E+F, H = C-G, percent = G/C, then retainage per line and
   the nine G702 lines from those.
4. **Submit.** Freeze a hashed snapshot of every input, so the application
   re-renders identically however far the schedule of values moves afterwards.

Only one period may be open per contract at a time, and a closed period cannot
be reopened -- it is a financial record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    ApplicationLine,
    ApplicationSnapshot,
    ApplicationStatus,
    Certification,
    ChangeOrder,
    ChangeOrderStatus,
    PrimeContract,
    RetainageRule,
    SovLine,
    StoredMaterial,
    User,
)
from massingbill.models.base import utcnow
from massingbill.services import audit, events, retainage
from massingbill.services import sov as sov_service
from massingbill.services.money import Cents, cents, percent_of


@dataclass(frozen=True)
class PeriodEntry:
    """What a user actually types for one line."""

    line_id: str
    this_period: Cents
    stored: Cents


# ── Lookups ─────────────────────────────────────────────────────────────────


def applications_for(contract: PrimeContract) -> list[Application]:
    return list(
        db.session.scalars(
            select(Application)
            .where(Application.prime_contract_id == contract.id)
            .order_by(Application.number)
        )
    )


def open_application(contract: PrimeContract) -> Application | None:
    """The one editable period, if there is one."""
    return db.session.scalar(
        select(Application)
        .where(
            Application.prime_contract_id == contract.id,
            Application.status.in_((ApplicationStatus.DRAFT, ApplicationStatus.REJECTED)),
        )
        .order_by(Application.number.desc())
        .limit(1)
    )


def previous_issued(contract: PrimeContract, before_number: int) -> Application | None:
    """The most recent issued application before this one.

    Draft and void periods are skipped: an abandoned draft never billed
    anything, so it must not affect the carry-forward.
    """
    return db.session.scalar(
        select(Application)
        .where(
            Application.prime_contract_id == contract.id,
            Application.number < before_number,
            Application.status.in_(
                (
                    ApplicationStatus.SUBMITTED,
                    ApplicationStatus.CERTIFIED,
                    ApplicationStatus.PAID,
                )
            ),
        )
        .order_by(Application.number.desc())
        .limit(1)
    )


def get_line(application: Application, line_id: str) -> ApplicationLine:
    line = db.session.scalar(
        select(ApplicationLine).where(
            ApplicationLine.id == line_id, ApplicationLine.application_id == application.id
        )
    )
    if line is None:
        raise NotFoundError("No such application line.")
    return line


# ── Opening a period ────────────────────────────────────────────────────────


def open_period(
    contract: PrimeContract,
    *,
    period_start: date,
    period_end: date,
    application_date: date | None = None,
    actor: User | None = None,
) -> Application:
    """Start the next requisition."""
    schedule = sov_service.approved_schedule(contract)
    if schedule is None:
        raise ConflictError(
            "The schedule of values must be approved before a period can be opened."
        )

    existing = open_application(contract)
    if existing is not None and existing.is_editable:
        raise ConflictError(
            f"Application #{existing.number} is still open. Submit or void it first."
        )

    if period_end < period_start:
        raise ValidationError("The period end cannot precede the period start.")

    live = _latest_live(contract)
    if live is not None and period_start <= live.period_end:
        raise ValidationError(
            f"This period overlaps application #{live.number}, which ends "
            f"{live.period_end.isoformat()}."
        )

    # Numbering counts voided applications; carry-forward does not. A void is a
    # record that an application existed and was withdrawn, so reusing its
    # number would make the log ambiguous -- but it billed nothing, so it must
    # not affect column D or line 7.
    number = next_number(contract)
    carried = _carry_forward(contract, number)

    application = Application(
        organization_id=contract.organization_id,
        prime_contract_id=contract.id,
        schedule_id=schedule.id,
        number=number,
        period_start=period_start,
        period_end=period_end,
        application_date=application_date or period_end,
        status=ApplicationStatus.DRAFT,
        form_style=str(contract.default_form_style),
    )
    db.session.add(application)
    db.session.flush()

    for sov_line in schedule.lines:
        db.session.add(
            ApplicationLine(
                organization_id=contract.organization_id,
                application_id=application.id,
                sov_line_id=sov_line.id,
                item_no=sov_line.item_no,
                description=sov_line.description,
                csi_code=sov_line.csi_code,
                sort_order=sov_line.sort_order,
                col_c_scheduled_value=sov_line.current_scheduled_value_cents,
                # Written once, from the preceding period. Never editable.
                col_d_previous=carried.get(sov_line.item_no, 0),
                col_e_this_period=0,
                col_f_stored=0,
                col_g_completed_stored=carried.get(sov_line.item_no, 0),
                col_h_balance=(
                    sov_line.current_scheduled_value_cents - carried.get(sov_line.item_no, 0)
                ),
                col_i_retainage=0,
                percent_complete_bp=0,
                is_co_line=sov_line.is_co_line,
            )
        )

    db.session.flush()
    recompute(application)

    audit.record(
        contract.organization_id,
        audit.APPLICATION_OPENED,
        entity_type="application",
        entity_id=application.id,
        after={"number": number, "period_end": period_end.isoformat()},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return application


def _latest_live(contract: PrimeContract) -> Application | None:
    """The most recent application that has not been voided.

    Used for the overlap check: a voided period's dates are free to reuse,
    because nothing was ever billed for them.
    """
    return db.session.scalar(
        select(Application)
        .where(
            Application.prime_contract_id == contract.id,
            Application.status != ApplicationStatus.VOID,
        )
        .order_by(Application.number.desc())
        .limit(1)
    )


def _carry_forward(contract: PrimeContract, number: int) -> dict[str, int]:
    """Column D for the new period, keyed by **item number**.

    Not by schedule-of-values line id. A revision copies every line into fresh
    rows with fresh ids, and a revision is exactly what a change order
    requires -- so keying on the id would silently zero column D on the first
    application after any change order, and the contractor would re-bill
    everything already billed. The item number is the identity a human uses,
    it is unique within a schedule, and the revision copy preserves it.
    """
    previous = previous_issued(contract, number)
    if previous is None:
        return {}
    return {line.item_no: line.carry_forward_cents for line in previous.lines}


# ── Entering and computing ──────────────────────────────────────────────────


def enter(
    application: Application, entries: list[PeriodEntry], *, actor: User | None = None
) -> None:
    """Record work completed and material stored for this period."""
    _require_editable(application)

    by_id = {line.id: line for line in application.lines}
    for entry in entries:
        line = by_id.get(entry.line_id)
        if line is None:
            raise NotFoundError(f"No line {entry.line_id} on this application.")
        line.col_e_this_period = int(entry.this_period)
        line.col_f_stored = int(entry.stored)

    db.session.flush()
    recompute(application)

    audit.record(
        application.organization_id,
        audit.APPLICATION_UPDATED,
        entity_type="application",
        entity_id=application.id,
        after={"lines": len(entries), "line8": application.line8_current_payment_due},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


def recompute(application: Application) -> Application:
    """Derive every G703 column and every G702 line from the entered values.

    Pure with respect to the database: it reads the lines and the rule, and
    writes the derived fields. Nothing here rounds twice.
    """
    contract = application.prime_contract
    rule = contract.retainage_rule or _default_rule(contract)

    # ── G703 columns ────────────────────────────────────────────────────────
    for line in application.lines:
        line.col_g_completed_stored = (
            line.col_d_previous + line.col_e_this_period + line.col_f_stored
        )
        line.col_h_balance = line.col_c_scheduled_value - line.col_g_completed_stored
        line.percent_complete_bp = percent_of(
            cents(line.col_g_completed_stored), cents(line.col_c_scheduled_value)
        )

    # ── Retainage, per line then summed ─────────────────────────────────────
    bases = [
        retainage.LineBasis(
            scheduled_value=cents(line.col_c_scheduled_value),
            work_to_date=cents(line.col_d_previous + line.col_e_this_period),
            stored=cents(line.col_f_stored),
            line_rate_bp=_line_rate(line),
        )
        for line in application.lines
    ]

    contract_sum = cents(sum(line.col_c_scheduled_value for line in application.lines))
    result = retainage.compute(rule, bases, contract_sum)

    for line, withheld in zip(application.lines, result.lines, strict=True):
        line.col_i_retainage = int(withheld.total)

    # ── The G702 header ─────────────────────────────────────────────────────
    approved = _approved_change_orders(contract)

    application.line1_original_sum = contract.original_contract_sum_cents
    application.line2_net_co = sum(co.amount_cents for co in approved)
    application.line3_contract_sum_to_date = (
        application.line1_original_sum + application.line2_net_co
    )
    application.line4_completed_stored = sum(
        line.col_g_completed_stored for line in application.lines
    )
    application.line5a_retainage_work = int(result.line5a_work)
    application.line5b_retainage_stored = int(result.line5b_stored)
    application.line5_total_retainage = int(result.total)
    application.line6_earned_less_retainage = (
        application.line4_completed_stored - application.line5_total_retainage
    )
    application.line7_previous_certificates = _previous_certificates(application)
    application.line8_current_payment_due = (
        application.line6_earned_less_retainage - application.line7_previous_certificates
    )
    application.line9_balance_to_finish = (
        application.line3_contract_sum_to_date - application.line6_earned_less_retainage
    )

    _fill_change_order_summary(application, approved)

    db.session.flush()
    return application


def _line_rate(line: ApplicationLine) -> int | None:
    sov_line = db.session.get(SovLine, line.sov_line_id)
    return sov_line.retainage_rate_bp if sov_line is not None else None


def _previous_certificates(application: Application) -> int:
    """G702 line 7 -- the prior *certificate*, not the prior request."""
    previous = previous_issued(application.prime_contract, application.number)
    return previous.certified_or_requested_cents if previous else 0


def _approved_change_orders(contract: PrimeContract) -> list[ChangeOrder]:
    return list(
        db.session.scalars(
            select(ChangeOrder)
            .where(
                ChangeOrder.prime_contract_id == contract.id,
                ChangeOrder.status == ChangeOrderStatus.APPROVED,
            )
            .order_by(ChangeOrder.number)
        )
    )


def _fill_change_order_summary(application: Application, approved: list[ChangeOrder]) -> None:
    """The G702 change-order box: what was approved before this period, and in it.

    Classified by **approval date falling inside the period**, which is what the
    form asks. An explicit ``applies_to_application_id`` overrides that, for the
    case where a change order is executed late but belongs to an earlier
    period's billing.
    """
    this_period: list[ChangeOrder] = []
    previous: list[ChangeOrder] = []
    for order in approved:
        if order.applies_to_application_id is not None:
            (this_period if order.applies_to_application_id == application.id else previous).append(
                order
            )
        elif order.approved_date is None:
            previous.append(order)
        elif application.period_start <= order.approved_date <= application.period_end:
            this_period.append(order)
        elif order.approved_date < application.period_start:
            previous.append(order)

    application.co_summary_prev_additions = sum(
        c.amount_cents for c in previous if c.amount_cents > 0
    )
    application.co_summary_prev_deductions = -sum(
        c.amount_cents for c in previous if c.amount_cents < 0
    )
    application.co_summary_this_additions = sum(
        c.amount_cents for c in this_period if c.amount_cents > 0
    )
    application.co_summary_this_deductions = -sum(
        c.amount_cents for c in this_period if c.amount_cents < 0
    )


def _default_rule(contract: PrimeContract) -> RetainageRule:
    """A contract with no rule withholds nothing, explicitly rather than by accident."""
    return RetainageRule(organization_id=contract.organization_id, rate_work_bp=0, rate_stored_bp=0)


# ── Stored materials ────────────────────────────────────────────────────────


def apply_stored_materials(application: Application) -> None:
    """Set column F from the stored-material records, and roll installed ones.

    Reading F from the records rather than trusting a typed number is what
    stops the classic double-bill: a material marked installed in this period
    leaves column F automatically, and its value has to be in column E to keep
    the line whole.
    """
    _require_editable(application)

    line_ids = [line.sov_line_id for line in application.lines]
    materials = list(
        db.session.scalars(
            select(StoredMaterial).where(
                StoredMaterial.sov_line_id.in_(line_ids),
                StoredMaterial.is_void.is_(False),
            )
        )
    )

    by_sov_line: dict[str, int] = {}
    for material in materials:
        if material.is_installed:
            continue  # rolled into column E; no longer stored
        by_sov_line[material.sov_line_id] = (
            by_sov_line.get(material.sov_line_id, 0) + material.value_cents
        )
        if material.first_billed_application_id is None:
            material.first_billed_application_id = application.id

    for line in application.lines:
        line.col_f_stored = by_sov_line.get(line.sov_line_id, 0)

    db.session.flush()
    recompute(application)


def install_material(material: StoredMaterial, application: Application) -> None:
    """Mark material as incorporated in this period.

    A single transaction: it leaves column F and the caller must account for it
    in column E. Doing this as two independent edits is how the same material
    gets billed twice.
    """
    _require_editable(application)
    if material.is_installed:
        raise ConflictError("That material has already been installed.")
    material.installed_in_application_id = application.id
    db.session.flush()


# ── Submitting and certifying ───────────────────────────────────────────────


def submit(
    application: Application, *, actor: User | None = None, actor_label: str = ""
) -> Application:
    """Freeze the period.

    Runs the tie-out engine first: an application that does not balance must
    not become a financial record.

    ``actor`` is optional because an API key is not a user and never will be --
    it outlives the person who minted it. When there is no user, ``actor_label``
    must name what acted instead, so the audit chain never records an anonymous
    submission.
    """
    if actor is None and not actor_label:
        raise ValidationError("A submission must record who made it.")
    _require_editable(application)

    from massingbill.services import tieout

    report = tieout.run(application)
    if report.blocking:
        # No webhook here on purpose. This path raises, the caller rolls back,
        # and an event queued inside that transaction would roll back with it --
        # the same reason the audit chain flushes rather than commits. The
        # ``tieout.failed`` event is emitted by the explicit check instead
        # (``services/events.py``), where the transaction survives.
        raise ValidationError(
            "This application does not tie out and cannot be submitted. "
            f"{len(report.blocking)} blocking issue(s): "
            + "; ".join(f.rule_id for f in report.blocking),
            details={"findings": [f.as_dict() for f in report.blocking]},
        )

    application.status = ApplicationStatus.SUBMITTED
    application.submitted_at = utcnow()
    application.submitted_by_id = actor.id if actor else None

    _take_snapshot(application)
    db.session.flush()

    audit.record(
        application.organization_id,
        audit.APPLICATION_SUBMITTED,
        entity_type="application",
        entity_id=application.id,
        after={
            "number": application.number,
            "line8_cents": application.line8_current_payment_due,
        },
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else actor_label,
    )
    events.application_submitted(application)
    return application


def certify(
    application: Application,
    amount_certified: Cents,
    *,
    certified_by_label: str,
    reason: str = "",
    actor: User | None = None,
) -> Certification:
    """Record the architect's certificate, which may differ from the request."""
    if application.status != ApplicationStatus.SUBMITTED:
        raise ConflictError("Only a submitted application can be certified.")

    certification = Certification(
        organization_id=application.organization_id,
        application_id=application.id,
        amount_certified_cents=int(amount_certified),
        variance_cents=int(amount_certified) - application.line8_current_payment_due,
        reason=reason,
        certified_by_id=actor.id if actor else None,
        certified_by_label=certified_by_label,
        certified_at=utcnow(),
    )
    db.session.add(certification)
    application.status = ApplicationStatus.CERTIFIED
    db.session.flush()

    audit.record(
        application.organization_id,
        audit.APPLICATION_CERTIFIED,
        entity_type="application",
        entity_id=application.id,
        after={
            "certified_cents": certification.amount_certified_cents,
            "variance_cents": certification.variance_cents,
        },
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    events.application_certified(application, certification)
    return certification


def void(application: Application, *, reason: str, actor: User | None = None) -> None:
    """Void a period. Financial records are never deleted."""
    if application.status == ApplicationStatus.PAID:
        raise ConflictError("A paid application cannot be voided.")

    application.status = ApplicationStatus.VOID
    application.note = f"{application.note}\nVoided: {reason}".strip()
    db.session.flush()

    audit.record(
        application.organization_id,
        audit.APPLICATION_VOIDED,
        entity_type="application",
        entity_id=application.id,
        after={"reason": reason},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


# ── Snapshots ───────────────────────────────────────────────────────────────


def snapshot_payload(application: Application) -> dict[str, object]:
    """Everything needed to re-render this application without the live rows."""
    contract = application.prime_contract
    rule = contract.retainage_rule

    return {
        "application": {
            "number": application.number,
            "period_start": application.period_start.isoformat(),
            "period_end": application.period_end.isoformat(),
            "application_date": application.application_date.isoformat(),
            "form_style": str(application.form_style),
            "line1_original_sum": application.line1_original_sum,
            "line2_net_co": application.line2_net_co,
            "line3_contract_sum_to_date": application.line3_contract_sum_to_date,
            "line4_completed_stored": application.line4_completed_stored,
            "line5a_retainage_work": application.line5a_retainage_work,
            "line5b_retainage_stored": application.line5b_retainage_stored,
            "line5_total_retainage": application.line5_total_retainage,
            "line6_earned_less_retainage": application.line6_earned_less_retainage,
            "line7_previous_certificates": application.line7_previous_certificates,
            "line8_current_payment_due": application.line8_current_payment_due,
            "line9_balance_to_finish": application.line9_balance_to_finish,
            "co_summary": {
                "prev_additions": application.co_summary_prev_additions,
                "prev_deductions": application.co_summary_prev_deductions,
                "this_additions": application.co_summary_this_additions,
                "this_deductions": application.co_summary_this_deductions,
            },
        },
        "contract": {
            "number": contract.number,
            "original_contract_sum": contract.original_contract_sum_cents,
        },
        "retainage_rule": (
            {
                "mode": str(rule.mode),
                "rate_work_bp": rule.rate_work_bp,
                "rate_stored_bp": rule.rate_stored_bp,
                "reduction_threshold_bp": rule.reduction_threshold_bp,
                "reduced_rate_bp": rule.reduced_rate_bp,
                "statutory_cap_bp": rule.statutory_cap_bp,
                "statute_citation": rule.statute_citation,
            }
            if rule is not None
            else None
        ),
        "lines": [
            {
                "item_no": line.item_no,
                "description": line.description,
                "csi_code": line.csi_code,
                "c": line.col_c_scheduled_value,
                "d": line.col_d_previous,
                "e": line.col_e_this_period,
                "f": line.col_f_stored,
                "g": line.col_g_completed_stored,
                "h": line.col_h_balance,
                "i": line.col_i_retainage,
                "percent_bp": line.percent_complete_bp,
            }
            for line in application.lines
        ],
    }


def frozen_fingerprint(application: Application) -> str:
    """Canonical JSON of only what this application itself owns.

    The distinction from :func:`snapshot_payload` matters. The payload is the
    *record*: it deliberately includes the contract and the retainage rule, so
    the document can be re-rendered without them. But those are shared, live
    rows -- a later period switching the contract to stepped retainage mutates
    the same rule object -- so re-deriving the payload later never reproduces
    it, and a drift check built on it would cry wolf on every historical
    application.

    The fingerprint covers the header and the lines: the numbers the
    application froze and nothing else. Re-deriving it must give the same
    answer forever, so a mismatch means somebody edited a submitted
    application, which is the only thing the check is meant to catch.
    """
    return json.dumps(
        {
            "number": application.number,
            "period_start": application.period_start.isoformat(),
            "period_end": application.period_end.isoformat(),
            "header": {
                "line1": application.line1_original_sum,
                "line2": application.line2_net_co,
                "line3": application.line3_contract_sum_to_date,
                "line4": application.line4_completed_stored,
                "line5a": application.line5a_retainage_work,
                "line5b": application.line5b_retainage_stored,
                "line5": application.line5_total_retainage,
                "line6": application.line6_earned_less_retainage,
                "line7": application.line7_previous_certificates,
                "line8": application.line8_current_payment_due,
                "line9": application.line9_balance_to_finish,
            },
            "lines": [
                {
                    "item_no": line.item_no,
                    "c": line.col_c_scheduled_value,
                    "d": line.col_d_previous,
                    "e": line.col_e_this_period,
                    "f": line.col_f_stored,
                    "g": line.col_g_completed_stored,
                    "h": line.col_h_balance,
                    "i": line.col_i_retainage,
                }
                for line in application.lines
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _take_snapshot(application: Application) -> ApplicationSnapshot:
    payload = json.dumps(
        snapshot_payload(application), sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(frozen_fingerprint(application).encode("utf-8")).hexdigest()

    snapshot = ApplicationSnapshot(
        organization_id=application.organization_id,
        application_id=application.id,
        payload=payload,
        sha256=digest,
        taken_at=utcnow(),
    )
    db.session.add(snapshot)
    db.session.flush()
    return snapshot


# ── Guards ──────────────────────────────────────────────────────────────────


def _require_editable(application: Application) -> None:
    if not application.is_editable:
        raise ConflictError(
            f"Application #{application.number} is {application.status} and cannot be changed. "
            "A submitted application is a financial record."
        )


def totals_check(contract: PrimeContract) -> dict[str, int]:
    """Across every issued period: the sum of payments due against line 3."""
    issued = [a for a in applications_for(contract) if a.is_issued]
    return {
        "applications": len(issued),
        "sum_line8_cents": sum(a.line8_current_payment_due for a in issued),
        "latest_line6_cents": issued[-1].line6_earned_less_retainage if issued else 0,
        "contract_sum_cents": (
            issued[-1].line3_contract_sum_to_date
            if issued
            else contract.original_contract_sum_cents
        ),
    }


def next_number(contract: PrimeContract) -> int:
    highest = db.session.scalar(
        select(func.max(Application.number)).where(Application.prime_contract_id == contract.id)
    )
    return (highest or 0) + 1


__all__ = [
    "PeriodEntry",
    "applications_for",
    "apply_stored_materials",
    "certify",
    "enter",
    "get_line",
    "install_material",
    "next_number",
    "open_application",
    "open_period",
    "previous_issued",
    "recompute",
    "snapshot_payload",
    "submit",
    "totals_check",
    "void",
]
