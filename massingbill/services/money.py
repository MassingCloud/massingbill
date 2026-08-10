"""The money kernel.

Every monetary computation in Massing Bill happens here. Nothing in this module
imports Flask, SQLAlchemy or anything else -- it is pure arithmetic over
integers, which is what makes it exhaustively testable and what lets the CI gate
in ``scripts/check_money_discipline.py`` say "scaling arithmetic on money belongs
in exactly one file" and mean it.

The rules (SPEC.md section 5), and why each one exists:

**Money is integer cents.** Floating point cannot represent ``0.10``. A schedule
of values with 200 lines, summed as floats, drifts. A pay app that is off by a
penny gets rejected, and the contractor waits another month to be paid.

**Percentages are basis points** (``int``, 1 bp = 0.01%). ``10%`` is ``1000``.
There is no float percent anywhere in the system, so a retainage rate cannot
arrive as ``0.09999999999999999``.

**Rounding happens once per computation, ``ROUND_HALF_UP``, away from zero on
ties.** Ties are resolved away from zero so that a credit rounds by the same
magnitude as the charge it reverses -- ``apply_bp(-x, r) == -apply_bp(x, r)``
holds for every input, which is asserted as a property.

**A total split across lines sums back to the total, exactly.** :func:`allocate`
uses largest-remainder so the residual cents land somewhere deterministic
instead of vanishing. This is the difference between "the math is approximately
right" and "the continuation sheet ties to the cover sheet".

**Retainage is computed per line and summed, never computed on the header and
pushed down.** That rule lives in the retainage service, but it is stated here
because this module is what makes it cheap: per-line ``apply_bp`` calls summed
with plain ``+`` are exact, so no reconciliation is needed in that direction.

``NewType`` is used deliberately rather than a bare alias. It costs a
``cents(...)`` call at the boundary and buys a type error when someone writes
``apply_bp(rate, amount)`` with the arguments the wrong way round -- a mistake
that is otherwise invisible and produces a plausible-looking number.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import NewType

__all__ = [
    "BP_SCALE",
    "ONE_HUNDRED_PERCENT",
    "ZERO",
    "Bp",
    "Cents",
    "MoneyError",
    "allocate",
    "apply_bp",
    "bp",
    "bp_from_percent",
    "cents",
    "format_bp",
    "negate",
    "parse_bp",
    "parse_money",
    "percent_from_bp",
    "percent_of",
    "residual",
    "split_evenly",
    "sum_cents",
    "to_decimal",
    "to_display",
]

#: An amount of money, in whole cents. Never a float, never a fraction of a cent.
Cents = NewType("Cents", int)

#: A rate in basis points. 1 bp = 0.01%; ``10%`` is ``1000``.
Bp = NewType("Bp", int)

#: Basis points in 100%.
BP_SCALE = 10_000

ONE_HUNDRED_PERCENT = Bp(BP_SCALE)
ZERO = Cents(0)

_MONEY_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")

#: Characters discarded before parsing an amount. Spelled with escapes because
#: two of them are invisible: a spreadsheet or a locale-formatted web page
#: routinely uses a non-breaking or narrow non-breaking space as the thousands
#: separator, and a pasted value containing one would otherwise be rejected as
#: "not a monetary amount" with nothing on screen to explain why.
_STRIPPED_FROM_AMOUNTS = ("$", ",", " ", chr(0x00A0), chr(0x202F), chr(0x2009))


class MoneyError(ValueError):
    """A monetary value or operation was invalid.

    Deliberately a plain ``ValueError`` subclass rather than one of the
    application's HTTP-aware errors: the kernel must not import the web layer.
    Callers translate it at their boundary.
    """


# ── Constructors ────────────────────────────────────────────────────────────


def cents(value: int) -> Cents:
    """Tag an integer as an amount in cents."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"Cents must be a whole number of cents, got {value!r}")
    return Cents(value)


def bp(value: int) -> Bp:
    """Tag an integer as a rate in basis points."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"Basis points must be a whole number, got {value!r}")
    return Bp(value)


def negate(amount: Cents) -> Cents:
    """Flip the sign. Credits and deductive change orders are negative cents."""
    return Cents(-amount)


def sum_cents(amounts: Iterable[Cents]) -> Cents:
    """Sum amounts. Addition of cents is exact, so no rounding is involved."""
    return Cents(sum(amounts))


def residual(total: Cents, parts: Sequence[Cents]) -> Cents:
    """``total`` minus the sum of ``parts``.

    Zero is the only acceptable answer anywhere a header is supposed to equal
    the sum of its lines; the tie-out engine reports anything else.
    """
    return Cents(total - sum(parts))


# ── Scaling ─────────────────────────────────────────────────────────────────


def apply_bp(amount: Cents, rate: Bp) -> Cents:
    """Apply a basis-point rate to an amount, rounding half away from zero.

    The single rounding site for rate application: retainage, percentages of
    completion, and anything else expressed as a rate goes through here.

    Computed in integers rather than :class:`~decimal.Decimal` because the exact
    quotient is available directly from ``divmod`` -- no context, no precision
    setting, no chance of a global ``Decimal`` context change altering a
    financial result at a distance.

        >>> apply_bp(cents(1_000_000), bp(1000))   # 10% of $10,000.00
        100000
        >>> apply_bp(cents(5), bp(5000))           # half of 5 cents, ties away from zero
        3
        >>> apply_bp(cents(-5), bp(5000))          # and symmetric for credits
        -3
    """
    if amount == 0 or rate == 0:
        return ZERO

    sign = -1 if (amount < 0) != (rate < 0) else 1
    quotient, remainder = divmod(abs(amount) * abs(rate), BP_SCALE)

    # Half or more rounds away from zero.
    if remainder * 2 >= BP_SCALE:
        quotient += 1

    return Cents(sign * quotient)


def percent_of(part: Cents, whole: Cents) -> Bp:
    """``part`` as a proportion of ``whole``, in basis points.

    This is the G703 percent-complete column (``G / C``). A line with a
    scheduled value of zero reports 0% rather than raising -- a zero-value line
    with work billed against it is a data error, and it is the tie-out engine's
    job to name it, not this function's job to crash mid-render.
    """
    if whole == 0:
        return Bp(0)

    sign = -1 if (part < 0) != (whole < 0) else 1
    quotient, remainder = divmod(abs(part) * BP_SCALE, abs(whole))
    if remainder * 2 >= abs(whole):
        quotient += 1

    return Bp(sign * quotient)


# ── Allocation ──────────────────────────────────────────────────────────────


def allocate(total: Cents, weights: Sequence[Cents]) -> list[Cents]:
    """Split ``total`` across ``weights``, summing back to ``total`` exactly.

    Largest-remainder apportionment. Each part is the exact proportional share
    rounded down; the cents left over are handed one each to the parts with the
    largest fractional remainder, ties broken by position so the result is
    deterministic and a re-run of the same period produces the same numbers.

    Handles negative totals (a deductive change order spread across lines) and
    zero weights (which never receive a cent).

        >>> allocate(cents(100), [cents(1), cents(1), cents(1)])
        [34, 33, 33]
        >>> sum(allocate(cents(-100), [cents(1), cents(1), cents(1)]))
        -100

    Raises:
        MoneyError: if the weights sum to zero while there is a non-zero total
            to distribute -- there is no meaningful answer, and inventing one
            would put money on an arbitrary line.
    """
    if not weights:
        if total == 0:
            return []
        raise MoneyError(f"Cannot allocate {total} cents across no lines")

    weight_total = sum(weights)

    if weight_total == 0:
        if total == 0:
            return [ZERO] * len(weights)
        raise MoneyError(f"Cannot allocate {total} cents proportionally: the weights sum to zero")

    # Allocate the magnitude and re-apply the sign, so that negating the total
    # negates every part rather than merely producing a list that happens to sum
    # correctly. Without this, floor division sends the residual cent in the
    # opposite direction for a negative total: allocate(1, [1, 1]) puts the odd
    # cent on line 1 while allocate(-1, [1, 1]) puts it on line 2. A change
    # order and the credit reversing it would then land on different lines,
    # leaving two SOV lines each a cent adrift while the totals still tie --
    # the hardest kind of discrepancy to find.
    if total < 0:
        return [Cents(-part) for part in allocate(Cents(-total), weights)]

    # Exact share of each line is (total * weight) / weight_total. Floor
    # division leaves a remainder whose sign matches weight_total and whose
    # magnitude is strictly less than it, so every remainder-over-total ratio
    # lies in [0, 1) regardless of the signs involved.
    bases: list[int] = []
    remainders: list[int] = []
    for weight in weights:
        numerator = total * weight
        base, remainder = divmod(numerator, weight_total)
        bases.append(base)
        remainders.append(remainder)

    leftover = total - sum(bases)

    # Rank by |remainder| descending, position ascending. All remainders share
    # weight_total's sign, so absolute value is the right comparison.
    order = sorted(range(len(weights)), key=lambda i: (-abs(remainders[i]), i))
    for index in order[:leftover]:
        bases[index] += 1

    return [Cents(value) for value in bases]


def split_evenly(total: Cents, parts: int) -> list[Cents]:
    """Split ``total`` into ``parts`` shares differing by at most one cent."""
    if parts <= 0:
        raise MoneyError(f"Cannot split into {parts} parts")
    return allocate(total, [Cents(1)] * parts)


# ── Parsing and display ─────────────────────────────────────────────────────


def parse_money(raw: str | int | Decimal, *, allow_rounding: bool = False) -> Cents:
    """Parse a human- or spreadsheet-supplied amount into cents.

    Accepts ``$1,234.56``, ``1234.56``, ``(1,234.56)`` for a negative in
    accounting notation, and a bare integer.

    By default a value with more than two decimal places is **rejected** rather
    than silently rounded, because silently rounding an amount someone typed
    into a pay application is how a cent goes missing without anyone noticing.
    Spreadsheet imports, where float noise like ``1234.5600000000001`` is
    routine and meaningless, pass ``allow_rounding=True``.
    """
    if isinstance(raw, bool):
        raise MoneyError(f"Not a monetary amount: {raw!r}")
    if isinstance(raw, int):
        return Cents(raw * 100)
    if isinstance(raw, Decimal):
        return _decimal_to_cents(raw, allow_rounding=allow_rounding, original=raw)

    text = raw.strip()
    if not text:
        raise MoneyError("Empty monetary amount")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    for noise in _STRIPPED_FROM_AMOUNTS:
        text = text.replace(noise, "")
    if text.startswith("+"):
        text = text[1:]

    if not _MONEY_PATTERN.match(text):
        raise MoneyError(f"Not a monetary amount: {raw!r}")

    if negative and text.startswith("-"):
        raise MoneyError(f"Ambiguous sign: {raw!r}")

    value = _decimal_to_cents(Decimal(text), allow_rounding=allow_rounding, original=raw)
    return Cents(-value) if negative else value


def _decimal_to_cents(value: Decimal, *, allow_rounding: bool, original: object) -> Cents:
    scaled = value * 100
    if scaled != scaled.to_integral_value():
        if not allow_rounding:
            raise MoneyError(
                f"{original!r} has sub-cent precision. Money is whole cents; "
                f"pass allow_rounding=True if this came from a spreadsheet."
            )
        # Half away from zero, matching apply_bp.
        sign = -1 if scaled < 0 else 1
        scaled = sign * (abs(scaled) + Decimal("0.5")).to_integral_value(rounding="ROUND_FLOOR")
    return Cents(int(scaled))


def to_decimal(amount: Cents) -> Decimal:
    """Exact decimal representation, for XLSX cells and JSON that needs a number.

    Always two decimal places. Plain division normalises the exponent away, so
    ``Decimal(32500000) / 100`` is ``Decimal('325000')`` -- numerically right,
    but it renders as ``325000`` in a CSV column where every other row shows
    cents, and a document whose amounts are formatted inconsistently invites
    exactly the suspicion this product exists to remove.
    """
    return (Decimal(amount) / 100).quantize(Decimal("0.01"))


def to_display(
    amount: Cents,
    *,
    symbol: str = "$",
    parens_for_negative: bool = False,
) -> str:
    """Format an amount for a document or a screen.

    ``parens_for_negative`` renders ``($1,234.56)``, the accounting convention a
    change-order log or a deduction column usually wants.

        >>> to_display(cents(123456789))
        '$1,234,567.89'
        >>> to_display(cents(-4200), parens_for_negative=True)
        '($42.00)'
    """
    negative = amount < 0
    whole, fraction = divmod(abs(amount), 100)
    body = f"{symbol}{whole:,}.{fraction:02d}"

    if not negative:
        return body
    return f"({body})" if parens_for_negative else f"-{body}"


def bp_from_percent(percent: str | int | Decimal) -> Bp:
    """Convert a percentage to basis points. ``"5.5"`` becomes ``550``.

    Rejects a percentage finer than 0.01%, because basis points cannot hold it
    and rounding a contractual retainage rate is not this function's decision.
    """
    if isinstance(percent, bool):
        raise MoneyError(f"Not a percentage: {percent!r}")
    value = Decimal(percent) if not isinstance(percent, Decimal) else percent
    scaled = value * 100
    if scaled != scaled.to_integral_value():
        raise MoneyError(
            f"{percent!r} is finer than one basis point (0.01%). "
            f"Rates are stored in whole basis points."
        )
    return Bp(int(scaled))


def parse_bp(raw: str | int | Decimal) -> Bp:
    """Parse ``"10%"``, ``"10"``, ``10`` or ``Decimal("5.5")`` into basis points."""
    if isinstance(raw, str):
        raw = raw.strip().rstrip("%").strip()
        if not raw:
            raise MoneyError("Empty percentage")
    return bp_from_percent(raw)


def percent_from_bp(rate: Bp) -> Decimal:
    """The exact percentage a basis-point rate represents."""
    return Decimal(rate) / 100


def format_bp(rate: Bp, *, places: int = 2) -> str:
    """Render a rate as a percentage: ``1000`` becomes ``'10.00%'``."""
    return f"{percent_from_bp(rate):.{places}f}%"
