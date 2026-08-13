"""Massing Bill -- standalone AIA-style GC billing engine.

Two things live here and nothing else: the version, and a lazy handle on the
application factory.

``massingbill.core`` must import with only the standard library reachable, and
importing any submodule runs this file first. A top-level ``from flask import
Flask`` here would therefore make the core drag Flask along and silently break
the property ``tests/test_architecture.py`` exists to hold -- a failure whose
error message points anywhere but at this file.

The factory itself is :mod:`massingbill.app`. Its contract, and the P0
acceptance criterion:

    A fresh clone with **no environment file and no network** boots, serves
    ``/healthz``, and selects the standalone entitlement provider and local
    storage. Nothing optional is imported unless an operator asks for it.

See SPEC.md sections 2 and 3, and docs/vendorable-core.md.
"""

from __future__ import annotations

from typing import Any

__version__ = "1.3.0"

__all__ = ["__version__", "create_app"]


def __getattr__(name: str) -> Any:
    """Resolve ``create_app`` on first use (PEP 562).

    Chosen over re-exporting so that ``from massingbill import create_app``
    keeps working unchanged for every caller, including the
    ``massingbill:create_app`` console entry point, while ``import
    massingbill.core`` stays free of Flask.
    """
    if name == "create_app":
        from massingbill.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
