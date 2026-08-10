"""Money kernel: worked examples and edge cases.

Property-based coverage lives in ``test_money_properties.py``. This file pins
the specific behaviours a reviewer should be able to read and check by hand,
including the ones drawn from real pay-application arithmetic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from massingbill.services.money import (
    BP_SCALE,
    MoneyError,
    allocate,
    apply_bp,
    bp,
    bp_from_percent,
    cents,
    format_bp,
    negate,
    parse_bp,
    parse_money,
    percent_from_bp,
    percent_of,
    residual,
    split_evenly,
    sum_cents,
    to_decimal,
    to_display,
)

# ── Constructors ────────────────────────────────────────────────────────────


def test_cents_rejects_a_float() -> None:
    """The entire point of the kernel. A float must never become an amount."""
    with pytest.raises(MoneyError):
        cents(12.34)  # type: ignore[arg-type]


def test_cents_rejects_a_bool() -> None:
    # bool is an int subclass; True would silently become one cent.
    with pytest.raises(MoneyError):
        cents(True)  # type: ignore[arg-type]


def test_bp_rejects_a_float() -> None:
    with pytest.raises(MoneyError):
        bp(10.5)  # type: ignore[arg-type]


def test_negate_flips_the_sign() -> None:
    assert negate(cents(4200)) == -4200
    assert negate(cents(-4200)) == 4200


def test_sum_cents_is_exact() -> None:
    assert sum_cents([cents(1), cents(2), cents(3)]) == 6
    assert sum_cents([]) == 0


def test_residual_is_zero_when_lines_tie_to_the_header() -> None:
    assert residual(cents(1000), [cents(600), cents(400)]) == 0
    assert residual(cents(1000), [cents(600), cents(399)]) == 1


# ── apply_bp ────────────────────────────────────────────────────────────────


def test_ten_percent_retainage_on_a_round_number() -> None:
    # $10,000.00 at 10% is $1,000.00
    assert apply_bp(cents(1_000_000), bp(1000)) == 100_000


def test_five_percent_retainage_matching_california_sb_61() -> None:
    # $1,234,567.89 at 5% is $61,728.39 (exactly 61728.3945, rounds up)
    assert apply_bp(cents(123_456_789), bp(500)) == 6_172_839


def test_ties_round_away_from_zero() -> None:
    # Half a cent, both directions.
    assert apply_bp(cents(5), bp(5000)) == 3
    assert apply_bp(cents(-5), bp(5000)) == -3


def test_a_credit_rounds_by_the_same_magnitude_as_the_charge() -> None:
    """A deductive change order must reverse a charge exactly, not off by a cent."""
    for amount in (1, 5, 7, 12345, 999_999, 1_000_003):
        assert apply_bp(cents(-amount), bp(333)) == -apply_bp(cents(amount), bp(333))


def test_a_zero_rate_withholds_nothing() -> None:
    assert apply_bp(cents(999_999), bp(0)) == 0


def test_a_zero_amount_yields_nothing() -> None:
    assert apply_bp(cents(0), bp(1000)) == 0


def test_one_hundred_percent_is_the_identity() -> None:
    assert apply_bp(cents(123_456_789), bp(BP_SCALE)) == 123_456_789


def test_a_negative_rate_flips_the_sign() -> None:
    assert apply_bp(cents(1000), bp(-1000)) == -100
    assert apply_bp(cents(-1000), bp(-1000)) == 100


# ── percent_of ──────────────────────────────────────────────────────────────


def test_percent_complete_on_a_g703_line() -> None:
    # Column G of $45,000.00 against a Column C of $180,000.00 is 25.00%.
    assert percent_of(cents(4_500_000), cents(18_000_000)) == 2500


def test_percent_complete_rounds_to_the_nearest_basis_point() -> None:
    assert percent_of(cents(1), cents(3)) == 3333
    assert percent_of(cents(2), cents(3)) == 6667


def test_a_zero_scheduled_value_reports_zero_percent() -> None:
    """A zero-value line is a data error for the tie-out engine, not a crash here."""
    assert percent_of(cents(500), cents(0)) == 0


def test_percent_of_handles_overbilling() -> None:
    # Billing more than the scheduled value is a policy violation, but the
    # arithmetic must still report it honestly so the rule engine can flag it.
    assert percent_of(cents(200), cents(100)) == 20_000


# ── allocate ────────────────────────────────────────────────────────────────


def test_the_classic_indivisible_split() -> None:
    assert allocate(cents(100), [cents(1), cents(1), cents(1)]) == [34, 33, 33]


def test_allocation_is_proportional() -> None:
    parts = allocate(cents(10_000), [cents(5000), cents(3000), cents(2000)])
    assert parts == [5000, 3000, 2000]


def test_a_deductive_change_order_spreads_across_lines_and_ties() -> None:
    parts = allocate(cents(-100), [cents(1), cents(1), cents(1)])
    assert sum(parts) == -100
    assert parts == [-34, -33, -33]


def test_a_credit_reverses_a_charge_line_by_line() -> None:
    """Found by the property suite: floor division used to send the residual
    cent the other way for a negative total, so a $0.01 charge landed on line 1
    while the $0.01 credit reversing it landed on line 2 -- two lines a cent
    adrift, with the totals still tying."""
    weights = [cents(1), cents(1)]
    assert allocate(cents(1), weights) == [1, 0]
    assert allocate(cents(-1), weights) == [-1, 0]


def test_zero_weight_lines_never_receive_a_cent() -> None:
    parts = allocate(cents(101), [cents(1), cents(0), cents(1), cents(0)])
    assert parts[1] == 0
    assert parts[3] == 0
    assert sum(parts) == 101


def test_ties_are_broken_by_position_so_reruns_are_identical() -> None:
    first = allocate(cents(10), [cents(1), cents(1), cents(1), cents(1)])
    second = allocate(cents(10), [cents(1), cents(1), cents(1), cents(1)])
    assert first == second == [3, 3, 2, 2]


def test_allocating_nothing_across_nothing() -> None:
    assert allocate(cents(0), []) == []


def test_allocating_something_across_nothing_is_an_error() -> None:
    with pytest.raises(MoneyError, match="across no lines"):
        allocate(cents(100), [])


def test_allocating_across_zero_weights_is_an_error() -> None:
    """Inventing a destination would silently put money on an arbitrary line."""
    with pytest.raises(MoneyError, match="weights sum to zero"):
        allocate(cents(100), [cents(0), cents(0)])


def test_allocating_zero_across_zero_weights_is_all_zeros() -> None:
    assert allocate(cents(0), [cents(0), cents(0)]) == [0, 0]


def test_a_realistic_schedule_of_values_allocation() -> None:
    # A $187,500.00 change order spread over three lines by scheduled value.
    weights = [cents(4_500_000), cents(2_250_000), cents(1_125_000)]
    parts = allocate(cents(18_750_000), weights)

    assert sum(parts) == 18_750_000
    assert parts == [10_714_286, 5_357_143, 2_678_571]


def test_split_evenly_differs_by_at_most_one_cent() -> None:
    parts = split_evenly(cents(1000), 7)
    assert sum(parts) == 1000
    assert max(parts) - min(parts) <= 1


def test_split_evenly_rejects_a_non_positive_count() -> None:
    with pytest.raises(MoneyError):
        split_evenly(cents(100), 0)


# ── Parsing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.56", 123_456),
        ("$1,234.56", 123_456),
        ("$ 1,234.56", 123_456),
        ("-1234.56", -123_456),
        ("(1,234.56)", -123_456),
        ("0.01", 1),
        ("0", 0),
        ("1234", 123_400),
        ("+1234.56", 123_456),
        ("1234.5", 123_450),
    ],
)
def test_parse_money_accepts_what_people_actually_type(raw: str, expected: int) -> None:
    assert parse_money(raw) == expected


@pytest.mark.parametrize(
    ("separator", "name"),
    [
        (chr(0x00A0), "no-break space"),
        (chr(0x202F), "narrow no-break space"),
        (chr(0x2009), "thin space"),
    ],
)
def test_invisible_thousands_separators_are_accepted(separator: str, name: str) -> None:
    """Spreadsheets and locale-formatted pages use these as thousands separators.
    Rejecting them produces "not a monetary amount" against a value that looks
    perfectly ordinary on screen, which is impossible for a user to debug."""
    assert parse_money(f"1{separator}234.56") == 123_456, name


def test_parse_money_accepts_a_bare_integer_as_dollars() -> None:
    assert parse_money(1234) == 123_400


def test_parse_money_accepts_a_decimal() -> None:
    assert parse_money(Decimal("1234.56")) == 123_456


def test_sub_cent_precision_is_rejected_by_default() -> None:
    """Silently rounding a typed amount is how a cent goes missing unnoticed."""
    with pytest.raises(MoneyError, match="sub-cent precision"):
        parse_money("1234.567")


def test_spreadsheet_float_noise_can_be_rounded_explicitly() -> None:
    assert parse_money("1234.5600000000001", allow_rounding=True) == 123_456
    assert parse_money(Decimal("1234.565"), allow_rounding=True) == 123_457
    assert parse_money(Decimal("-1234.565"), allow_rounding=True) == -123_457


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1,2,3.4.5", "$", "--12", "12-", "1.2.3"])
def test_parse_money_rejects_nonsense(raw: str) -> None:
    with pytest.raises(MoneyError):
        parse_money(raw)


def test_parse_money_rejects_an_ambiguous_double_negative() -> None:
    with pytest.raises(MoneyError, match="Ambiguous sign"):
        parse_money("(-1234.56)")


def test_parse_money_rejects_a_bool() -> None:
    with pytest.raises(MoneyError):
        parse_money(True)  # type: ignore[arg-type]


# ── Display ─────────────────────────────────────────────────────────────────


def test_display_groups_thousands() -> None:
    assert to_display(cents(123_456_789)) == "$1,234,567.89"


def test_display_pads_the_cents() -> None:
    assert to_display(cents(5)) == "$0.05"
    assert to_display(cents(100)) == "$1.00"
    assert to_display(cents(0)) == "$0.00"


def test_display_negatives_two_ways() -> None:
    assert to_display(cents(-4200)) == "-$42.00"
    assert to_display(cents(-4200), parens_for_negative=True) == "($42.00)"


def test_display_accepts_another_symbol() -> None:
    assert to_display(cents(4200), symbol="") == "42.00"


def test_to_decimal_is_exact() -> None:
    assert to_decimal(cents(123_456_789)) == Decimal("1234567.89")
    assert to_decimal(cents(-5)) == Decimal("-0.05")


def test_to_decimal_always_shows_two_places() -> None:
    """A whole-dollar amount must not render as ``325000`` in a column where
    every other row shows cents. Plain division normalises the exponent away."""
    assert str(to_decimal(cents(32_500_000))) == "325000.00"
    assert str(to_decimal(cents(0))) == "0.00"
    assert str(to_decimal(cents(100))) == "1.00"
    assert str(to_decimal(cents(-4200))) == "-42.00"


# ── Basis points ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("percent", "expected"),
    [("10", 1000), ("5", 500), ("5.5", 550), ("0.01", 1), ("100", 10_000), ("0", 0)],
)
def test_percentages_convert_to_basis_points(percent: str, expected: int) -> None:
    assert bp_from_percent(percent) == expected


def test_a_rate_finer_than_a_basis_point_is_rejected() -> None:
    """Rounding a contractual retainage rate is not this function's decision."""
    with pytest.raises(MoneyError, match="finer than one basis point"):
        bp_from_percent("0.125")


def test_bp_from_percent_rejects_a_bool() -> None:
    with pytest.raises(MoneyError):
        bp_from_percent(True)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["10%", "10", " 10 % ", 10, Decimal("10")])
def test_parse_bp_accepts_the_usual_forms(raw: object) -> None:
    assert parse_bp(raw) == 1000  # type: ignore[arg-type]


def test_parse_bp_rejects_an_empty_percentage() -> None:
    with pytest.raises(MoneyError, match="Empty percentage"):
        parse_bp("%")


def test_basis_points_render_as_a_percentage() -> None:
    assert format_bp(bp(1000)) == "10.00%"
    assert format_bp(bp(550)) == "5.50%"
    assert format_bp(bp(1000), places=0) == "10%"


def test_percent_from_bp_is_exact() -> None:
    assert percent_from_bp(bp(550)) == Decimal("5.5")
