"""Retainage.

**Retainage is computed per line and summed. Never computed on the header and
pushed down.** Header-down allocation is the single most common source of the
one-cent disagreements that get pay applications rejected, because the header
rounds once and the lines round again against a different base. Per-line
``apply_bp`` calls summed with plain addition are exact, and the sum *is* the
header by construction.

Four modes, all of which exist in real contracts:

``FLAT``
    One rate on everything -- completed work and stored material alike.

``SPLIT``
    The G702 default: line 5a withholds against completed work (columns D+E),
    line 5b against stored material (column F), at separate rates. Owners
    routinely withhold less on material they can repossess than on labour they
    cannot.

``VARIABLE_LINE``
    A per-line rate, which is what G703 column I exists for. Used when some
    scopes carry different retention -- a bonded subcontractor's work at 5%
    while everything else sits at 10%.

``STEPPED``
    The rate drops once the project passes a completion threshold; "10% to 50%
    complete, 5% thereafter" is the common form. The reduced rate applies to
    the whole balance going forward, not only to work performed after the
    threshold, which is what makes a stepped period's retainage go *down*.
"""

from __future__ import annotations

from dataclasses import dataclass

from massingbill.models import RetainageMode, RetainageRule
from massingbill.services.money import Bp, Cents, apply_bp, bp, cents, percent_of


@dataclass(frozen=True)
class LineBasis:
    """What one line contributes to the retainage computation."""

    scheduled_value: Cents
    work_to_date: Cents  # columns D + E
    stored: Cents  # column F
    line_rate_bp: int | None = None  # only for VARIABLE_LINE


@dataclass(frozen=True)
class LineRetainage:
    work: Cents
    stored: Cents

    @property
    def total(self) -> Cents:
        return cents(self.work + self.stored)


@dataclass(frozen=True)
class RetainageResult:
    """The per-line withholdings and the header they sum to."""

    lines: list[LineRetainage]
    line5a_work: Cents
    line5b_stored: Cents
    effective_work_rate_bp: Bp

    @property
    def total(self) -> Cents:
        return cents(self.line5a_work + self.line5b_stored)


def effective_rates(
    rule: RetainageRule, *, completed_stored: Cents, contract_sum: Cents
) -> tuple[Bp, Bp]:
    """The rates that apply to this period, after any stepped reduction.

    Completion is measured against the contract sum to date, which is what the
    threshold in a stepped clause refers to.
    """
    work_rate = bp(rule.rate_work_bp)
    stored_rate = bp(rule.rate_stored_bp if rule.mode != RetainageMode.FLAT else rule.rate_work_bp)

    if rule.mode == RetainageMode.STEPPED and rule.reduction_threshold_bp is not None:
        complete = percent_of(completed_stored, contract_sum)
        if complete >= rule.reduction_threshold_bp:
            reduced = bp(rule.reduced_rate_bp if rule.reduced_rate_bp is not None else 0)
            work_rate = reduced
            stored_rate = reduced

    return work_rate, stored_rate


def compute(rule: RetainageRule, bases: list[LineBasis], contract_sum: Cents) -> RetainageResult:
    """Withhold per line, then sum. The header is the sum, not a second sum."""
    completed_stored = cents(sum(basis.work_to_date + basis.stored for basis in bases))
    work_rate, stored_rate = effective_rates(
        rule, completed_stored=completed_stored, contract_sum=contract_sum
    )

    lines: list[LineRetainage] = []
    for basis in bases:
        if rule.mode == RetainageMode.VARIABLE_LINE:
            # Column I: the line carries its own rate. A line without one is
            # withheld at the contract rate rather than at zero -- silently
            # withholding nothing because a field was left blank is the wrong
            # failure direction.
            rate = bp(basis.line_rate_bp if basis.line_rate_bp is not None else rule.rate_work_bp)
            lines.append(
                LineRetainage(
                    work=apply_bp(basis.work_to_date, rate),
                    stored=apply_bp(basis.stored, rate),
                )
            )
        else:
            lines.append(
                LineRetainage(
                    work=apply_bp(basis.work_to_date, work_rate),
                    stored=apply_bp(basis.stored, stored_rate),
                )
            )

    return RetainageResult(
        lines=lines,
        line5a_work=cents(sum(line.work for line in lines)),
        line5b_stored=cents(sum(line.stored for line in lines)),
        effective_work_rate_bp=work_rate,
    )


def exceeds_statutory_cap(rule: RetainageRule, result: RetainageResult, base: Cents) -> bool:
    """True when the effective withholding is above the jurisdiction's cap.

    Measured against what was actually withheld rather than the nominal rate: a
    contract can name 10% and still withhold 5% because of a stepped reduction,
    and only the money withheld matters to the statute.
    """
    if rule.statutory_cap_bp is None or base == 0:
        return False
    return percent_of(result.total, base) > rule.statutory_cap_bp
