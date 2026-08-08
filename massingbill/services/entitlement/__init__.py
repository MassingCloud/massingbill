"""Entitlement adapters.

``get_provider`` is the only place in the codebase that knows a non-standalone
provider exists, and it imports one lazily -- so an install that never opts in
never imports the adapter, and deleting the adapter module leaves the core
importable (the ``no-adapters`` CI job proves it).
"""

from __future__ import annotations

from typing import Any

from massingbill.errors import AdapterUnavailableError

from .base import UNLIMITED, Entitlement, EntitlementProvider, SeatResult, Seats
from .standalone import StandaloneProvider

__all__ = [
    "UNLIMITED",
    "Entitlement",
    "EntitlementProvider",
    "SeatResult",
    "Seats",
    "StandaloneProvider",
    "get_provider",
]


def get_provider(name: str, **options: Any) -> EntitlementProvider:
    if name == "standalone":
        return StandaloneProvider(**options)
    if name == "massing_cloud":
        try:
            from .massing_cloud import MassingCloudProvider
        except ImportError as exc:  # pragma: no cover - exercised in adapter tests
            raise AdapterUnavailableError(
                "The massing.cloud entitlement adapter is not installed. "
                "Install it with: pip install 'massingbill[oidc]'"
            ) from exc
        return MassingCloudProvider(**options)
    raise ValueError(f"Unknown entitlement provider: {name!r}")
