"""Tests for the money-discipline gate.

A gate nobody has verified is a gate that silently stops working. These plant
deliberate violations and assert the checker catches each one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_money_discipline import PACKAGE, check, main


def _check_source(tmp_path: Path, source: str, *, relative_name: str = "offender.py") -> list[str]:
    """Run the checker over a synthetic module placed inside the package tree."""
    target = PACKAGE / relative_name
    target.write_text(source, encoding="utf-8")
    try:
        return check(target, PACKAGE)
    finally:
        target.unlink()


def test_the_real_package_is_clean() -> None:
    assert main([str(PACKAGE)]) == 0


def test_multiplying_money_outside_the_kernel_is_caught(tmp_path: Path) -> None:
    findings = _check_source(
        tmp_path,
        "def bad(scheduled_value_cents: int, rate: int) -> int:\n"
        "    return scheduled_value_cents * rate // 10000\n",
    )
    assert findings, "multiplying cents outside the kernel must be reported"
    assert "apply_bp()" in findings[0]


def test_dividing_money_outside_the_kernel_is_caught(tmp_path: Path) -> None:
    findings = _check_source(
        tmp_path,
        "def bad(total_amount: int, parts: int) -> int:\n    return total_amount / parts\n",
    )
    assert findings


def test_adding_cents_is_allowed(tmp_path: Path) -> None:
    """Addition of cents is exact; only rounding-introducing operators are policed."""
    findings = _check_source(
        tmp_path,
        "def fine(work_cents: int, stored_cents: int) -> int:\n"
        "    return work_cents + stored_cents\n",
    )
    assert findings == []


def test_float_columns_are_rejected(tmp_path: Path) -> None:
    findings = _check_source(
        tmp_path,
        "import sqlalchemy as sa\n\n\nclass T:\n    amount = sa.Float()\n",
    )
    assert any("Float is never valid" in f for f in findings)


def test_numeric_on_the_money_path_is_rejected(tmp_path: Path) -> None:
    findings = _check_source(
        tmp_path,
        "import sqlalchemy as sa\n\n\nclass T:\n    retainage = sa.Numeric(12, 2)\n",
    )
    assert any("Numeric is never valid" in f for f in findings)


def test_declaring_a_money_column_directly_is_rejected(tmp_path: Path) -> None:
    findings = _check_source(
        tmp_path,
        "from sqlalchemy.orm import mapped_column\n\n\n"
        "class T:\n    contract_sum_cents = mapped_column()\n",
    )
    assert any("money_column()" in f for f in findings)


def test_the_kernel_itself_may_do_arithmetic() -> None:
    """services/money.py is the one place scaling is allowed (it does not exist yet)."""
    from scripts.check_money_discipline import KERNEL

    assert "massingbill/services/money.py" in KERNEL


def test_main_reports_failure_on_a_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    offender = PACKAGE / "offender_main.py"
    offender.write_text("x = total_amount * 2\n", encoding="utf-8")
    try:
        assert main([str(offender)]) == 1
    finally:
        offender.unlink()

    assert "Money discipline violations" in capsys.readouterr().err
