"""The G702/G703 computation, tested with nothing installed.

Deliberately **not** pytest. These ship with the vendored core, and the repos
that consume it do not all have pytest -- modelmaker runs
``python test_<name>.py`` and expects each file to assert for itself. Ten of
massingplan's vendored suites failed on exactly that, so this file uses plain
``assert`` and a ``__main__`` runner and imports only the standard library.

The upstream suite in ``tests/`` still uses pytest and is far larger. This is
the copy that travels, and it covers the arithmetic a consumer is relying on.

Run:  python massingbill/core/tests/test_mb_requisition.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import the core whether this file is run in place or vendored beside it.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from massingbill.core.enums import RetainageMode
from massingbill.core.money import cents
from massingbill.core.requisition import LineEntry, compute
from massingbill.core.retainage import RetainageSpec


def three_lines() -> list[LineEntry]:
    return [
        LineEntry("001", cents(100_000_00), description="Sitework"),
        LineEntry("002", cents(250_000_00), description="Concrete"),
        LineEntry("003", cents(150_000_00), description="Steel"),
    ]


def test_an_empty_period_is_all_zeroes() -> None:
    app = compute(three_lines(), original_contract_sum=cents(500_000_00))

    assert app.line4_completed_stored == 0
    assert app.line5_total_retainage == 0
    assert app.line8_current_payment_due == 0
    assert app.line9_balance_to_finish == 500_000_00
    assert app.ties_out()


def test_the_columns_add_up_across() -> None:
    """G = D + E + F, and H = C - G, on every line."""
    entries = three_lines()
    entries[0] = LineEntry(
        "001",
        cents(100_000_00),
        previous=cents(20_000_00),
        this_period=cents(30_000_00),
        stored=cents(5_000_00),
    )
    app = compute(entries, original_contract_sum=cents(500_000_00))
    line = app.lines[0]

    assert line.col_g_completed_stored == 55_000_00
    assert line.col_h_balance == 45_000_00
    assert line.percent_complete_bp == 5500  # 55.00%


def test_retainage_is_withheld_per_line_and_summed() -> None:
    """The header is the sum of the lines, not a second computation of it.

    Withholding on the header and apportioning down is where a cover sheet and
    its continuation sheet start disagreeing by a penny.
    """
    entries = [
        LineEntry("001", cents(100_000_00), this_period=cents(33_333_33)),
        LineEntry("002", cents(100_000_00), this_period=cents(33_333_33)),
        LineEntry("003", cents(100_000_00), this_period=cents(33_333_34)),
    ]
    app = compute(
        entries,
        original_contract_sum=cents(300_000_00),
        retainage=RetainageSpec(rate_work_bp=1000, rate_stored_bp=1000),
    )

    assert app.line5_total_retainage == sum(line.col_i_retainage for line in app.lines)
    assert app.ties_out()


def test_the_header_identities_hold() -> None:
    app = compute(
        [LineEntry("001", cents(100_000_00), this_period=cents(50_000_00))],
        original_contract_sum=cents(100_000_00),
        net_change_orders=cents(10_000_00),
        previous_certificates=cents(5_000_00),
        retainage=RetainageSpec(rate_work_bp=500, rate_stored_bp=500),
    )

    assert app.line3_contract_sum_to_date == 110_000_00
    assert app.line4_completed_stored == 50_000_00
    assert app.line5_total_retainage == 2_500_00
    assert app.line6_earned_less_retainage == 47_500_00
    assert app.line8_current_payment_due == 42_500_00
    assert app.line9_balance_to_finish == 62_500_00
    assert app.ties_out()


def test_a_deductive_change_order_lowers_the_contract_sum() -> None:
    app = compute(
        three_lines(),
        original_contract_sum=cents(500_000_00),
        net_change_orders=cents(-62_000_00),
    )
    assert app.line3_contract_sum_to_date == 438_000_00


def test_stored_material_is_retained_at_its_own_rate() -> None:
    """SPLIT mode: line 5a and 5b can differ, which is why the form has both."""
    app = compute(
        [
            LineEntry(
                "001", cents(100_000_00), this_period=cents(40_000_00), stored=cents(20_000_00)
            )
        ],
        original_contract_sum=cents(100_000_00),
        retainage=RetainageSpec(mode=RetainageMode.SPLIT, rate_work_bp=1000, rate_stored_bp=0),
    )

    assert app.line5a_retainage_work == 4_000_00
    assert app.line5b_retainage_stored == 0
    assert app.line5_total_retainage == 4_000_00


def test_a_stepped_rate_drops_past_the_threshold() -> None:
    spec = RetainageSpec(
        mode=RetainageMode.STEPPED,
        rate_work_bp=1000,
        rate_stored_bp=1000,
        reduction_threshold_bp=5000,
        reduced_rate_bp=500,
    )

    below = compute(
        [LineEntry("001", cents(100_000_00), this_period=cents(40_000_00))],
        original_contract_sum=cents(100_000_00),
        retainage=spec,
    )
    above = compute(
        [LineEntry("001", cents(100_000_00), this_period=cents(60_000_00))],
        original_contract_sum=cents(100_000_00),
        retainage=spec,
    )

    assert below.effective_retainage_rate_bp == 1000
    assert above.effective_retainage_rate_bp == 500
    assert above.line5_total_retainage == 3_000_00


def test_a_variable_line_without_a_rate_falls_back_to_the_contract_rate() -> None:
    """Not to zero. Silently withholding nothing because a field was blank is
    the wrong direction to fail in."""
    app = compute(
        [
            LineEntry("001", cents(100_000_00), this_period=cents(10_000_00), line_rate_bp=500),
            LineEntry("002", cents(100_000_00), this_period=cents(10_000_00)),
        ],
        original_contract_sum=cents(200_000_00),
        retainage=RetainageSpec(mode=RetainageMode.VARIABLE_LINE, rate_work_bp=1000),
    )

    assert app.lines[0].col_i_retainage == 500_00
    assert app.lines[1].col_i_retainage == 1_000_00


def test_a_zero_value_line_does_not_divide_by_zero() -> None:
    app = compute([LineEntry("001", cents(0))], original_contract_sum=cents(0))
    assert app.lines[0].percent_complete_bp == 0
    assert app.ties_out()


def test_no_amount_is_ever_a_float() -> None:
    """The property the whole core exists for."""
    app = compute(
        [LineEntry("001", cents(100_000_00), this_period=cents(33_333_33))],
        original_contract_sum=cents(100_000_00),
        retainage=RetainageSpec(rate_work_bp=333),
    )

    values = [
        app.line1_original_sum,
        app.line4_completed_stored,
        app.line5_total_retainage,
        app.line8_current_payment_due,
        *[line.col_i_retainage for line in app.lines],
    ]
    for value in values:
        assert isinstance(value, int), f"{value!r} is not an int"
        assert not isinstance(value, bool)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a runner reports, it does not raise
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")

    print(f"{len(tests) - failed}/{len(tests)} passed in {Path(__file__).name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
