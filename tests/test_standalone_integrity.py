"""Tests that keep the standalone promise honest.

"Standalone with an optional cloud" is a claim that decays the moment somebody
takes a shortcut. SPEC.md section 13 answers that with three gates; two of them
live here, and the third (``no-adapters``) is a CI job that deletes the adapter
modules and re-runs this suite.
"""

from __future__ import annotations

import importlib
import pkgutil
import socket

import pytest
from flask import Flask

import massingbill
from massingbill.errors import AdapterUnavailableError
from massingbill.services.entitlement import StandaloneProvider, get_provider
from massingbill.services.storage import LocalStorage, get_backend

#: Modules that are allowed to know massing.cloud exists. Everything else in the
#: package must be importable and testable without them.
ADAPTER_MODULES = {
    "massingbill.services.entitlement.massing_cloud",
    "massingbill.services.identity.oidc",
    "massingbill.services.storage.s3",
    "massingbill.services.storage.massing_vault",
}


def _iter_core_modules() -> list[str]:
    names: list[str] = []
    for info in pkgutil.walk_packages(massingbill.__path__, prefix="massingbill."):
        if info.name in ADAPTER_MODULES:
            continue
        if info.name.startswith("massingbill.services.integrations"):
            continue
        names.append(info.name)
    return names


def test_every_core_module_imports_without_optional_dependencies() -> None:
    """No core module may need boto3, Authlib, requests or a network."""
    for name in _iter_core_modules():
        importlib.import_module(name)


def test_no_core_module_imports_an_adapter() -> None:
    """The import-linter contract, asserted at runtime too.

    Importing the whole core must not drag an adapter module into sys.modules.
    """
    import sys

    for name in _iter_core_modules():
        importlib.import_module(name)

    leaked = ADAPTER_MODULES & set(sys.modules)
    assert not leaked, f"core import pulled in optional adapters: {sorted(leaked)}"


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


def test_selecting_a_missing_adapter_fails_with_a_useful_message() -> None:
    """P9 has not shipped, so asking for it must explain rather than crash."""
    with pytest.raises(AdapterUnavailableError) as exc:
        get_provider("massing_cloud")
    assert "pip install" in str(exc.value)


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
