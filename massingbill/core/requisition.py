"""The G702/G703 computation, over plain values.

This is the arithmetic of a monthly requisition with nothing else attached: no
ORM, no session, no framework. Give it a contract sum, a list of schedule lines
with what was entered against them, a retainage policy and what was previously
certified, and it returns every G703 column and every G702 header line.

**Why it is separate from ``services/application.py``.** That module owns
persistence -- loading rows, flushing, auditing, freezing a period. This owns
the sums. Keeping them apart means the arithmetic can be exercised, vendored and
reasoned about without a database in scope, and it means the numbers a
contractor is paid on do not depend on a web framework being importable.

Everything is integer cents and basis points; see ``core/money.py`` for why.
Nothing here rounds twice: retainage is withheld per line and summed, and the
header is that sum rather than a second, independent computation of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from massingbill.core.money import Bp, Cents, cents, percent_of
from massingbill.core.retainage import LineBasis, RetainageSpec
from massingbill.core.retainage import compute as compute_retainage

__all__ = [
    "Application",
    "ApplicationLine",
    "LineEntry",
    "compute",
]

#: A named zero. ``cents(0)`` is a ``NewType`` call -- a no-op returning an
#: immutable int -- but as a dataclass default it reads to a linter as a shared
#: mutable, and the reader has to know about ``NewType`` to be sure it is not.
ZERO = cents(0)
NO_PERCENT = Bp(0)


@dataclass(frozen=True)
class LineEntry:
    """One schedule-of-values line, plus what was entered against it.

    ``previous`` is column D -- everything billed on earlier applications. It is
    passed in rather than derived, because on a revised schedule of values the
    line that carries it forward is matched by item number rather than by
    identity, and only the caller knows that mapping.
    """

    item_no: str
    scheduled_value: Cents
    previous: Cents = ZERO
    this_period: Cents = ZERO
    stored: Cents = ZERO
    #: Only consulted in ``VARIABLE_LINE`` mode.
    line_rate_bp: int | None = None
    description: str = ""


@dataclass(frozen=True)
class ApplicationLine:
    """One row of the G703 continuation sheet, columns A through I."""

    item_no: str
    description: str
    col_c_scheduled_value: Cents
    col_d_previous: Cents
    col_e_this_period: Cents
    col_f_stored: Cents
    col_g_completed_stored: Cents
    col_h_balance: Cents
    col_i_retainage: Cents
    percent_complete_bp: Bp


@dataclass(frozen=True)
class Application:
    """The G702 header, and the continuation sheet it is the sum of.

    Line 5 is split into 5a (completed work) and 5b (stored material) because
    the two are commonly withheld at different rates, and a form that reports
    only the total cannot be checked against a contract that names both.
    """

    lines: list[ApplicationLine] = field(default_factory=list)

    line1_original_sum: Cents = ZERO
    line2_net_co: Cents = ZERO
    line3_contract_sum_to_date: Cents = ZERO
    line4_completed_stored: Cents = ZERO
    line5a_retainage_work: Cents = ZERO
    line5b_retainage_stored: Cents = ZERO
    line5_total_retainage: Cents = ZERO
    line6_earned_less_retainage: Cents = ZERO
    line7_previous_certificates: Cents = ZERO
    line8_current_payment_due: Cents = ZERO
    line9_balance_to_finish: Cents = ZERO

    effective_retainage_rate_bp: Bp = NO_PERCENT

    def ties_out(self) -> bool:
        """The three identities that must hold on every application.

        Not the full rule set -- that lives in ``services/tieout.py`` and knows
        about contracts, waivers and statutes. These are the ones that are
        purely arithmetic, so a caller with no database can still assert the
        sheet is internally consistent.
        """
        return (
            self.line3_contract_sum_to_date == self.line1_original_sum + self.line2_net_co
            and self.line4_completed_stored
            == sum(line.col_g_completed_stored for line in self.lines)
            and self.line5_total_retainage
            == self.line5a_retainage_work + self.line5b_retainage_stored
            and self.line5_total_retainage == sum(line.col_i_retainage for line in self.lines)
            and self.line6_earned_less_retainage
            == self.line4_completed_stored - self.line5_total_retainage
            and self.line8_current_payment_due
            == self.line6_earned_less_retainage - self.line7_previous_certificates
        )


@dataclass(frozen=True)
class _Derived:
    """Columns G, H and the percentage, before retainage is known."""

    entry: LineEntry
    completed_stored: Cents
    balance: Cents
    percent_complete: Bp


def _derive(entry: LineEntry) -> _Derived:
    completed_stored = cents(entry.previous + entry.this_period + entry.stored)
    return _Derived(
        entry=entry,
        completed_stored=completed_stored,
        balance=cents(entry.scheduled_value - completed_stored),
        percent_complete=percent_of(completed_stored, entry.scheduled_value),
    )


def compute(
    entries: list[LineEntry],
    *,
    original_contract_sum: Cents,
    retainage: RetainageSpec | None = None,
    net_change_orders: Cents = ZERO,
    previous_certificates: Cents = ZERO,
) -> Application:
    """Derive every G703 column and every G702 line from what was entered.

    ``original_contract_sum`` is the contract as signed; ``net_change_orders``
    is the approved movement since. They are separate arguments because the
    G702 reports them on separate lines and an owner reconciles them separately
    -- collapsing them into one number loses the thing line 2 exists to show.
    """
    spec = retainage or RetainageSpec()

    # ── G703 columns ────────────────────────────────────────────────────────
    columns = [_derive(entry) for entry in entries]

    # ── Retainage: per line, then summed. Never the other way round. ────────
    #
    # Withholding on the header and apportioning down is where the one-cent
    # disagreements between a cover sheet and its continuation sheet come from.
    bases = [
        LineBasis(
            scheduled_value=entry.scheduled_value,
            work_to_date=cents(entry.previous + entry.this_period),
            stored=entry.stored,
            line_rate_bp=entry.line_rate_bp,
        )
        for entry in entries
    ]
    scheduled_total = cents(sum(entry.scheduled_value for entry in entries))
    withheld = compute_retainage(spec, bases, scheduled_total)

    lines = [
        ApplicationLine(
            item_no=column.entry.item_no,
            description=column.entry.description,
            col_c_scheduled_value=column.entry.scheduled_value,
            col_d_previous=column.entry.previous,
            col_e_this_period=column.entry.this_period,
            col_f_stored=column.entry.stored,
            col_g_completed_stored=column.completed_stored,
            col_h_balance=column.balance,
            col_i_retainage=withheld_line.total,
            percent_complete_bp=column.percent_complete,
        )
        for column, withheld_line in zip(columns, withheld.lines, strict=True)
    ]

    # ── The G702 header ─────────────────────────────────────────────────────
    line3 = cents(original_contract_sum + net_change_orders)
    line4 = cents(sum(line.col_g_completed_stored for line in lines))
    line5 = withheld.total
    line6 = cents(line4 - line5)
    line8 = cents(line6 - previous_certificates)

    return Application(
        lines=lines,
        line1_original_sum=original_contract_sum,
        line2_net_co=net_change_orders,
        line3_contract_sum_to_date=line3,
        line4_completed_stored=line4,
        line5a_retainage_work=withheld.line5a_work,
        line5b_retainage_stored=withheld.line5b_stored,
        line5_total_retainage=line5,
        line6_earned_less_retainage=line6,
        line7_previous_certificates=previous_certificates,
        line8_current_payment_due=line8,
        line9_balance_to_finish=cents(line3 - line6),
        effective_retainage_rate_bp=withheld.effective_work_rate_bp,
    )
