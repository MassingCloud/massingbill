"""The Massing Bill core: the calculation, with nothing attached to it.

**Zero runtime dependencies. Standard library only.** No Flask, no SQLAlchemy,
no HTTP client, no third-party package of any kind. ``tests/test_architecture.py``
fails the build if that stops being true, because a property nobody checks is a
property that decays silently and is discovered by whoever tries to vendor it
next.

Everything above this line -- ``models/``, ``services/``, ``blueprints/`` --
is an adapter: persistence, workflow and transport over these functions. The
arithmetic that decides what a contractor is paid does not depend on a web
framework being importable, and can be copied into another codebase without
dragging one along.

What lives here:

- :mod:`massingbill.core.money` -- integer cents, basis points, one rounding
  site, largest-remainder allocation.
- :mod:`massingbill.core.retainage` -- withholding per line, in four modes,
  summed to the header rather than apportioned down from it.
- :mod:`massingbill.core.requisition` -- the G702 header and the G703
  continuation sheet, from plain values.
- :mod:`massingbill.core.enums` -- the vocabulary the arithmetic branches on.

The rule for anything added here: if it needs a database session, a request or
a config object, it belongs in ``services/`` instead.
"""

from __future__ import annotations

from massingbill.core.enums import RetainageMode
from massingbill.core.money import (
    Bp,
    Cents,
    MoneyError,
    allocate,
    apply_bp,
    bp,
    cents,
    format_bp,
    parse_bp,
    parse_money,
    percent_of,
    split_evenly,
    to_decimal,
    to_display,
)
from massingbill.core.requisition import Application, ApplicationLine, LineEntry
from massingbill.core.requisition import compute as compute_application
from massingbill.core.retainage import (
    LineBasis,
    LineRetainage,
    RetainageResult,
    RetainageSpec,
    effective_rates,
    exceeds_statutory_cap,
)
from massingbill.core.retainage import compute as compute_retainage

__all__ = [
    "Application",
    "ApplicationLine",
    "Bp",
    "Cents",
    "LineBasis",
    "LineEntry",
    "LineRetainage",
    "MoneyError",
    "RetainageMode",
    "RetainageResult",
    "RetainageSpec",
    "allocate",
    "apply_bp",
    "bp",
    "cents",
    "compute_application",
    "compute_retainage",
    "effective_rates",
    "exceeds_statutory_cap",
    "format_bp",
    "parse_bp",
    "parse_money",
    "percent_of",
    "split_evenly",
    "to_decimal",
    "to_display",
]
