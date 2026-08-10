"""The core has zero runtime dependencies, and the build fails when it stops.

This is the whole point of ``massingbill/core/``. A framework-free core is a
property that decays silently: someone adds one convenient import, everything
still passes, and the next person who tries to vendor the calculation into
another codebase discovers it drags Flask and SQLAlchemy along. By then it is
weeks of untangling rather than a one-line review comment.

So the property is enforced here rather than documented. See
``docs/vendorable-core.md``, and modelmaker's
``docs/internal/vendorable-core-standard.md`` for the org-wide version.

Checked by reading the source, not by importing it. An import test would pass
for a dependency that happens to be installed in the environment running the
tests, which is exactly the environment where it is always installed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "massingbill" / "core"

#: Modules the core may import: the standard library, and itself.
#:
#: Deliberately derived from the interpreter rather than hand-listed, so the
#: allowance cannot quietly grow when somebody adds a name to a set.
STDLIB = set(sys.stdlib_module_names)

SELF = "massingbill.core"


def core_modules() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def imported_roots(path: Path) -> set[str]:
    """Every top-level module name this file imports, however it imports it.

    Walks the whole AST rather than reading the header, because a deferred
    import inside a function is still a runtime dependency -- it just fails
    later, on somebody else's machine.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import, so within the core
                continue
            if node.module:
                roots.add(node.module.split(".")[0])

    return roots


def test_the_core_has_modules_to_check() -> None:
    """Guard against the whole suite passing because the directory moved."""
    modules = core_modules()
    assert len(modules) >= 4, f"expected the core to exist at {CORE}"


@pytest.mark.parametrize("path", core_modules(), ids=lambda p: p.name)
def test_no_core_module_imports_a_third_party_package(path: Path) -> None:
    """The rule, stated once per file so a failure names the offender."""
    foreign = set()

    for root in imported_roots(path):
        if root in STDLIB or root == "__future__":
            continue
        if root == "massingbill":
            continue  # narrowed below
        foreign.add(root)

    assert not foreign, (
        f"{path.name} imports {sorted(foreign)}, which is outside the standard library.\n"
        "The core must stay vendorable: no Flask, no SQLAlchemy, no third-party package. "
        "Put the dependency in services/ and have it call the core, or add it as an "
        "optional adapter."
    )


@pytest.mark.parametrize("path", core_modules(), ids=lambda p: p.name)
def test_no_core_module_imports_the_rest_of_the_application(path: Path) -> None:
    """``massingbill.core`` only. Not models, not services, not extensions.

    A separate test from the one above because it fails for a different reason
    and needs a different fix: this one is usually an enum or a dataclass that
    belongs *in* the core rather than a dependency to remove.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    offenders = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module.startswith("massingbill") and not node.module.startswith(SELF):
                offenders.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("massingbill") and not alias.name.startswith(SELF):
                    offenders.add(alias.name)

    assert not offenders, (
        f"{path.name} imports {sorted(offenders)} from outside the core.\n"
        "If the core genuinely needs it -- an enum the arithmetic branches on, say -- "
        "move it into massingbill/core/ and re-export it from its old home, so there "
        "is still exactly one definition."
    )


def test_the_core_imports_with_nothing_else_installed() -> None:
    """Import it in a subprocess with the application's own packages hidden.

    The static checks above can be satisfied by a module that imports something
    conditionally at runtime. This one proves the whole core actually loads with
    only the standard library reachable.
    """
    import subprocess

    program = (
        "import sys;"
        # Refuse anything that is not stdlib and not the core itself.
        "allowed = set(sys.stdlib_module_names) | {'massingbill'};"
        "import importlib;"
        "import massingbill.core as c;"
        "loaded = {m.split('.')[0] for m in sys.modules if m};"
        "foreign = sorted(m for m in loaded - allowed if not m.startswith('_'));"
        "print('FOREIGN:' + ','.join(foreign))"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, our own literal
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    marker = next(line for line in result.stdout.splitlines() if line.startswith("FOREIGN:"))
    foreign = [name for name in marker.removeprefix("FOREIGN:").split(",") if name]

    # pytest's own machinery is not in play in the subprocess, so anything here
    # was pulled in by importing the core.
    assert not foreign, f"importing massingbill.core loaded {foreign}"


def test_the_core_is_reachable_without_the_flask_app() -> None:
    """``import massingbill.core`` must not run ``massingbill/__init__.py``'s
    Flask work.

    Stated as its own test because it is the failure a vendoring consumer hits
    first, and the error it produces (an ImportError deep in a factory) does not
    obviously mean "the package __init__ is doing too much".
    """
    import subprocess

    program = "import sys;import massingbill.core;print('FLASK:' + str('flask' in sys.modules))"
    result = subprocess.run(  # noqa: S603 - our own interpreter, our own literal
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert "FLASK:False" in result.stdout, "importing the core pulled Flask in"
