"""Property-based tests for the money kernel.

Worked examples (``test_money.py``) prove the cases someone thought of. These
prove the invariants that must hold for *every* input, which is the only way to
be confident about an allocator that has to stay exact across a twelve-month
project with two hundred lines and a mid-stream deductive change order.

Amount bounds are set at roughly a trillion dollars in cents -- far beyond any
real contract sum, but large enough that a latent overflow or precision
assumption would show.
"""

from __future__ import annotations

import random
from decimal import Decimal
from fractions import Fraction

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from massingbill.services.money import (
    BP_SCALE,
    allocate,
    apply_bp,
    bp,
    bp_from_percent,
    cents,
    parse_money,
    percent_from_bp,
    percent_of,
    split_evenly,
    to_decimal,
    to_display,
)

AMOUNTS = st.integers(min_value=-(10**14), max_value=10**14).map(cents)
NON_NEGATIVE_AMOUNTS = st.integers(min_value=0, max_value=10**14).map(cents)
RATES = st.integers(min_value=-BP_SCALE, max_value=BP_SCALE).map(bp)
NON_NEGATIVE_RATES = st.integers(min_value=0, max_value=BP_SCALE).map(bp)
WEIGHT_LISTS = st.lists(
    st.integers(min_value=0, max_value=10**12).map(cents), min_size=1, max_size=60
)

# ── allocate ────────────────────────────────────────────────────────────────


@given(total=AMOUNTS, weights=WEIGHT_LISTS)
def test_allocation_always_sums_back_to_the_total(total: int, weights: list[int]) -> None:
    """The invariant the continuation sheet depends on. Nothing else matters if this fails."""
    assume(sum(weights) != 0)
    assert sum(allocate(cents(total), [cents(w) for w in weights])) == total


@given(total=AMOUNTS, weights=WEIGHT_LISTS)
def test_every_part_is_within_a_cent_of_its_exact_share(total: int, weights: list[int]) -> None:
    """Largest-remainder must not merely sum correctly -- it must be fair."""
    assume(sum(weights) != 0)
    weight_total = sum(weights)
    parts = allocate(cents(total), [cents(w) for w in weights])

    for part, weight in zip(parts, weights, strict=True):
        exact = Fraction(total * weight, weight_total)
        assert abs(Fraction(part) - exact) < 1


@given(total=AMOUNTS, weights=WEIGHT_LISTS, factor=st.integers(min_value=1, max_value=10_000))
def test_allocation_is_invariant_under_rescaling_the_weights(
    total: int, weights: list[int], factor: int
) -> None:
    """Weights are proportions. Expressing the same SOV in cents or in dollars
    must not move a penny between lines."""
    assume(sum(weights) != 0)
    original = allocate(cents(total), [cents(w) for w in weights])
    rescaled = allocate(cents(total), [cents(w * factor) for w in weights])
    assert original == rescaled


@given(total=AMOUNTS, weights=WEIGHT_LISTS)
def test_zero_weight_lines_never_receive_money(total: int, weights: list[int]) -> None:
    assume(sum(weights) != 0)
    parts = allocate(cents(total), [cents(w) for w in weights])
    for part, weight in zip(parts, weights, strict=True):
        if weight == 0:
            assert part == 0


@given(total=AMOUNTS, weights=WEIGHT_LISTS)
def test_allocation_is_deterministic(total: int, weights: list[int]) -> None:
    """Re-running a period must reproduce the period, cent for cent."""
    assume(sum(weights) != 0)
    amounts = [cents(w) for w in weights]
    assert allocate(cents(total), amounts) == allocate(cents(total), amounts)


@given(total=AMOUNTS, weights=WEIGHT_LISTS)
def test_negating_the_total_negates_every_part(total: int, weights: list[int]) -> None:
    """A credit must reverse the charge it offsets, line by line, not just in sum."""
    assume(sum(weights) != 0)
    amounts = [cents(w) for w in weights]
    positive = allocate(cents(total), amounts)
    negative = allocate(cents(-total), amounts)
    assert [-p for p in positive] == negative


@given(total=AMOUNTS, parts=st.integers(min_value=1, max_value=200))
def test_an_even_split_is_even(total: int, parts: int) -> None:
    shares = split_evenly(cents(total), parts)
    assert sum(shares) == total
    assert max(shares) - min(shares) <= 1


# ── apply_bp ────────────────────────────────────────────────────────────────


@given(amount=AMOUNTS, rate=RATES)
def test_rate_application_is_sign_symmetric(amount: int, rate: int) -> None:
    """Retainage on a credit is the exact reverse of retainage on the charge."""
    assert apply_bp(cents(-amount), bp(rate)) == -apply_bp(cents(amount), bp(rate))


@given(amount=AMOUNTS, rate=RATES)
def test_rate_application_never_errs_by_more_than_half_a_cent(amount: int, rate: int) -> None:
    exact = Fraction(amount * rate, BP_SCALE)
    assert abs(Fraction(apply_bp(cents(amount), bp(rate))) - exact) <= Fraction(1, 2)


@given(amount=AMOUNTS)
def test_a_zero_rate_withholds_nothing(amount: int) -> None:
    assert apply_bp(cents(amount), bp(0)) == 0


@given(amount=AMOUNTS)
def test_a_full_rate_is_the_identity(amount: int) -> None:
    assert apply_bp(cents(amount), bp(BP_SCALE)) == amount


@given(
    amount=NON_NEGATIVE_AMOUNTS,
    low=NON_NEGATIVE_RATES,
    high=NON_NEGATIVE_RATES,
)
def test_a_higher_rate_never_withholds_less(amount: int, low: int, high: int) -> None:
    assume(low <= high)
    assert apply_bp(cents(amount), bp(low)) <= apply_bp(cents(amount), bp(high))


# ── percent_of ──────────────────────────────────────────────────────────────


@given(part=AMOUNTS, whole=AMOUNTS)
def test_percent_of_never_errs_by_more_than_half_a_basis_point(part: int, whole: int) -> None:
    assume(whole != 0)
    exact = Fraction(part * BP_SCALE, whole)
    assert abs(Fraction(percent_of(cents(part), cents(whole))) - exact) <= Fraction(1, 2)


@given(whole=NON_NEGATIVE_AMOUNTS)
def test_a_line_billed_in_full_reads_one_hundred_percent(whole: int) -> None:
    assume(whole != 0)
    assert percent_of(cents(whole), cents(whole)) == BP_SCALE


# ── Parsing and display ─────────────────────────────────────────────────────


@given(amount=AMOUNTS)
def test_display_and_parse_round_trip(amount: int) -> None:
    """What a user reads back must parse to exactly what was stored."""
    assert parse_money(to_display(cents(amount), symbol="")) == amount


@given(amount=AMOUNTS)
def test_accounting_notation_round_trips_too(amount: int) -> None:
    rendered = to_display(cents(amount), symbol="", parens_for_negative=True)
    assert parse_money(rendered) == amount


@given(amount=AMOUNTS)
def test_to_decimal_round_trips(amount: int) -> None:
    assert parse_money(to_decimal(cents(amount))) == amount


@given(rate=st.integers(min_value=-BP_SCALE, max_value=BP_SCALE))
def test_basis_points_round_trip_through_percent(rate: int) -> None:
    assert bp_from_percent(percent_from_bp(bp(rate))) == rate


@given(percent=st.decimals(min_value=Decimal("-100"), max_value=Decimal("100"), places=2))
def test_two_decimal_percentages_always_convert(percent: Decimal) -> None:
    assert percent_from_bp(bp_from_percent(percent)) == percent


# ── The acceptance criterion, stated literally ──────────────────────────────


@settings(deadline=None)
@given(st.just(None))
def test_allocation_holds_across_ten_thousand_generated_cases(_: None) -> None:
    """SPEC.md P1 acceptance: 10,000 generated cases.

    A seeded loop rather than a hypothesis run, so the exact ten thousand cases
    are the same on every machine and in every CI run. Hypothesis covers the
    adversarial edges above; this covers breadth reproducibly.
    """
    rng = random.Random(20260808)

    for _case in range(10_000):
        line_count = rng.randint(1, 40)
        weights = [cents(rng.randint(0, 50_000_000)) for _ in range(line_count)]
        if sum(weights) == 0:
            weights[rng.randrange(line_count)] = cents(rng.randint(1, 50_000_000))

        total = cents(rng.randint(-500_000_000, 500_000_000))
        parts = allocate(total, weights)

        assert sum(parts) == total
        assert len(parts) == line_count
