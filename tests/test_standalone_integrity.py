"""Tests that keep the standalone promise honest.

"Standalone with an optional cloud" is a claim that decays the moment somebody
takes a shortcut. SPEC.md section 13 answers that with three gates; two of them
live here, and the third (``no-adapters``) is a CI job that deletes the adapter
modules and re-runs this suite.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import socket

import pytest
from flask import Flask

import massingbill
from massingbill import optional as _optional
from massingbill.errors import AdapterUnavailableError
from massingbill.services.entitlement import StandaloneProvider, get_provider
from massingbill.services.storage import LocalStorage, get_backend

# Single-sourced in massingbill/optional.py, because three places have to
# agree about this: here, the `offline` CI job, and anyone reasoning about what
# a minimal deployment installs. When the CI job kept its own copy it walked a
# module needing an extra it had not installed, and failed on a fact this test
# already knew.
ADAPTER_MODULES = set(_optional.ADAPTER_MODULES)
OPTIONAL_MODULES = set(_optional.OPTIONAL_MODULES)


def _iter_core_modules() -> list[str]:
    return [
        info.name
        for info in pkgutil.walk_packages(massingbill.__path__, prefix="massingbill.")
        if not _optional.is_optional(info.name)
    ]


def test_every_core_module_imports_without_optional_dependencies() -> None:
    """No core module may need boto3, Authlib, requests or a network."""
    for name in _iter_core_modules():
        importlib.import_module(name)


def test_no_core_module_imports_an_adapter() -> None:
    """The import-linter contract, asserted at runtime too.

    Run in a **subprocess**. In-process, this measured whatever the test session
    happened to have imported already -- so it passed for as long as no other
    test touched an adapter, and started failing the moment one did, reporting a
    leak that was not there. A test that depends on the order of the suite is
    not testing the property it names.
    """
    import subprocess
    import sys

    program = (
        "import importlib, sys;"
        "import massingbill.optional as o;"
        "mods = __import__('tests.test_standalone_integrity', fromlist=['x'])"
        ".core_module_names();"
        "[importlib.import_module(m) for m in mods];"
        "leaked = o.ADAPTER_MODULES & set(sys.modules);"
        # Marked, because importing the package prints a WeasyPrint banner to
        # stdout when the native stack is absent. Reading the whole stream
        # reported that banner as a leaked module name.
        "print('LEAKED:' + ','.join(sorted(leaked)))"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, our own literal
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
    )
    marker = next(line for line in result.stdout.splitlines() if line.startswith("LEAKED:"))
    leaked = [name for name in marker.removeprefix("LEAKED:").split(",") if name]
    assert not leaked, f"core import pulled in optional adapters: {leaked}"


def core_module_names() -> list[str]:
    """Public alias, so the subprocess above can reach the same list."""
    return _iter_core_modules()


def test_the_renderers_package_imports_without_its_extras(app: Flask) -> None:
    """A headless deployment installs neither WeasyPrint nor openpyxl.

    Importing the renderers package must still work, report honestly what it
    can produce, and explain what is missing -- rather than exploding at import
    and taking the whole application down with it.
    """
    from massingbill.services.renderers import (
        PDF_AVAILABLE,
        XLSX_AVAILABLE,
        available_formats,
        pdf_unavailable_reason,
        xlsx_unavailable_reason,
    )
    from massingbill.services.renderers.documents import Format

    formats = available_formats()

    # These three need nothing beyond the core, so they are always on offer.
    assert Format.CSV in formats
    assert Format.JSON in formats
    assert Format.HTML in formats

    if not PDF_AVAILABLE:
        assert "WeasyPrint" in pdf_unavailable_reason()
        assert Format.PDF not in formats
    if not XLSX_AVAILABLE:
        assert "openpyxl" in xlsx_unavailable_reason()
        assert Format.XLSX not in formats


def test_default_adapters_are_the_standalone_ones(app: Flask) -> None:
    assert isinstance(app.extensions["massingbill_entitlement"], StandaloneProvider)
    assert isinstance(app.extensions["massingbill_storage"], LocalStorage)


def test_standalone_entitlement_grants_everything() -> None:
    entitlement = StandaloneProvider().effective("org-1")

    assert entitlement.entitled is True
    assert entitlement.read_only is False
    assert entitlement.tier == "standalone"
    assert entitlement.allows("gc_billing") is True
    assert entitlement.allows("anything_at_all") is True
    assert entitlement.within("billing_projects", current=10_000) is True
    assert entitlement.seats.exhausted is False


def test_standalone_seat_claim_always_succeeds() -> None:
    result = StandaloneProvider().claim_seat("org-1", "user-1", "instance-1")
    assert result.granted is True


def test_selecting_an_adapter_without_configuring_it_says_so() -> None:
    """P9 has shipped. What still has to hold is that selecting a provider and
    configuring one are different acts, and the first without the second fails
    loudly at startup rather than quietly at somebody's first page load.

    ``test_adapters.py`` covers the not-installed case by forcing the
    ImportError, which does not depend on which phase we are in.
    """
    with pytest.raises((ValueError, TypeError, AdapterUnavailableError)):
        get_provider("massing_cloud")


def test_unknown_adapter_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown entitlement provider"):
        get_provider("not-a-provider")
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_backend("not-a-backend")


def test_the_network_guard_actually_blocks(block_network: None) -> None:
    """Guard the guard: if this stops raising, every other test's isolation is a lie."""
    from tests.conftest import NetworkBlockedError

    with pytest.raises(NetworkBlockedError):
        socket.socket().connect(("example.com", 80))
