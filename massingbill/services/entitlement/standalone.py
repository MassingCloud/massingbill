"""The default entitlement provider: no enforcement at all.

A self-hosted install has no licence, no phone-home, no telemetry and no kill
switch (SPEC.md 3.3). This provider exists so the rest of the application can
ask entitlement questions unconditionally and always get "yes" -- which keeps
the gating call sites identical whether or not an operator later opts into a
commercial provider.

An operator who wants to impose their own internal caps can still do so by
passing ``limits`` when constructing this provider; nothing here reads the
network either way.
"""

from __future__ import annotations

from typing import Any

from .base import Entitlement, EntitlementProvider, SeatResult, Seats


class StandaloneProvider(EntitlementProvider):
    """Grants everything. The default, and the only provider the core tests use."""

    name = "standalone"

    def __init__(self, limits: dict[str, Any] | None = None, seat_limit: int = -1) -> None:
        self._limits = dict(limits or {})
        self._seat_limit = seat_limit

    def effective(self, organization_id: str) -> Entitlement:
        return Entitlement(
            tier="standalone",
            entitled=True,
            status="active",
            expires_at=None,
            seats=Seats(limit=self._seat_limit, used=0),
            limits=dict(self._limits),
            source="standalone",
        )

    def claim_seat(self, organization_id: str, user_id: str, instance: str) -> SeatResult:
        return SeatResult(granted=True, seats=Seats(limit=self._seat_limit, used=0))
