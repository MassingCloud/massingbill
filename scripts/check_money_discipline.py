#!/usr/bin/env python
"""Fail the build when money arithmetic escapes the money kernel.

SPEC.md section 5: monetary values are integer cents, all arithmetic on them
goes through ``massingbill/services/money.py``, and there is exactly one
rounding site per computation. That discipline is only real if something checks
it, because the violation that costs you a rejected pay app is a single stray
``line_total * rate`` in a template helper written at 5pm.

Two things are checked:

1. **No float money.** ``Float``/``float`` on anything named like money, and any
   monetary column not declared through ``money_column``.
2. **No arithmetic on money outside the kernel.** ``*``, ``/``, ``//`` and ``%``
   applied to a money-named operand anywhere but ``services/money.py``.

Addition and subtraction of cents are exact and are allowed; multiplication and
division are where rounding enters, so those are what is policed.

Usage::

    python scripts/check_money_discipline.py [paths...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "massingbill"

#: Modules allowed to perform money arithmetic.
KERNEL = {"massingbill/services/money.py"}

#: Modules allowed to declare monetary columns directly.
COLUMN_DECLARERS = {"massingbill/models/base.py"}

MONEY_HINTS = ("_cents", "amount", "_sum", "_value", "retainage", "price", "total")

#: Suffixes that rule a name out however money-ish it reads. A foreign key to
#: the retainage rule is not money; a rate in basis points is not money; a
#: timestamp is not money. Without these the gate cries wolf, and a gate people
#: learn to ignore is worse than no gate.
NOT_MONEY_SUFFIXES = ("_id", "_bp", "_at", "_by", "_count", "_mode", "_rule")

SCALING_OPS = (ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)


def _is_money_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(NOT_MONEY_SUFFIXES):
        return False
    return any(hint in lowered for hint in MONEY_HINTS)


def _operand_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class MoneyVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.path = relative_path
        self.problems: list[tuple[int, str]] = []
        self.in_kernel = relative_path in KERNEL
        self.may_declare_columns = relative_path in COLUMN_DECLARERS

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not self.in_kernel and isinstance(node.op, SCALING_OPS):
            for side in (node.left, node.right):
                name = _operand_name(side)
                if name and _is_money_name(name):
                    self.problems.append(
                        (
                            node.lineno,
                            f"money arithmetic on {name!r} outside services/money.py -- "
                            f"use apply_bp() or allocate()",
                        )
                    )
                    break
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_column(node.target, node.value, node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_column(target, node.value, node.lineno)
        self.generic_visit(node)

    def _check_column(self, target: ast.AST, value: ast.AST | None, lineno: int) -> None:
        name = _operand_name(target)
        if not name or not _is_money_name(name) or value is None:
            return
        if not isinstance(value, ast.Call):
            return

        func = value.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in {"mapped_column", "Column"}:
            return
        if self.may_declare_columns:
            return

        self.problems.append(
            (
                lineno,
                f"monetary column {name!r} declared directly -- "
                f"use models.base.money_column() so the cents decision stays greppable",
            )
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in {"Float", "Numeric", "REAL", "DOUBLE_PRECISION"}:
            self.problems.append(
                (
                    node.lineno,
                    f"{node.attr} is never valid on the money path -- use BigInteger cents",
                )
            )
        self.generic_visit(node)


def check(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root.parent).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a syntax error fails elsewhere
        return [f"{relative}:{exc.lineno}: could not parse: {exc.msg}"]

    visitor = MoneyVisitor(relative)
    visitor.visit(tree)
    return [f"{relative}:{line}: {message}" for line, message in visitor.problems]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    targets = [Path(a) for a in args] or [PACKAGE]

    findings: list[str] = []
    for target in targets:
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        findings.extend(f for path in files for f in check(path, PACKAGE))

    if findings:
        print("Money discipline violations (SPEC.md section 5):\n", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nMonetary values are integer cents. Multiplication and division "
            "belong in services/money.py, where rounding happens once and is "
            "followed by penny reconciliation.",
            file=sys.stderr,
        )
        return 1

    print(f"Money discipline: clean ({len(targets)} target(s) checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
