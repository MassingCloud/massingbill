"""The entitlement seam.

One of the three pluggable adapters described in SPEC.md 3.2. The application
asks *this* interface what a customer may do; it never learns where the answer
came from. ``StandaloneProvider`` is the default and answers "everything, no
limits" without touching the network.

The ``Entitlement`` dataclass deliberately mirrors the field names and semantics
of the massing.cloud entitlement object (``tier``, ``entitled``, ``limits``,
``seats``, ``expires_at`` -- massingcoud/docs/11-cloud-integration-spec.md
section 4). Matching the shape costs nothing today and means an optional adapter
can be dropped in later without touching a single call site.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: ``-1`` means unlimited, following the massing convention.
UNLIMITED = -1


@dataclass(frozen=True)
class Seats:
    limit: int = UNLIMITED
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.limit != UNLIMITED and self.used >= self.limit


@dataclass(frozen=True)
class Entitlement:
    """What a customer is allowed to do, from whatever source decided it."""

    tier: str = "standalone"
    entitled: bool = True
    status: str = "active"
    expires_at: datetime | None = None
    seats: Seats = field(default_factory=Seats)
    limits: dict[str, Any] = field(default_factory=dict)
    source: str = "standalone"

    def limit(self, name: str, default: Any = UNLIMITED) -> Any:
        return self.limits.get(name, default)

    def allows(self, capability: str) -> bool:
        """True when a boolean capability flag is granted."""
        if not self.entitled:
            return False
        return bool(self.limits.get(capability, True))

    def within(self, name: str, current: int) -> bool:
        """True when a counted resource is still under its cap."""
        cap = self.limit(name)
        return cap == UNLIMITED or current < int(cap)

    @property
    def read_only(self) -> bool:
        """A lapsed subscription degrades to read-only; data is never destroyed."""
        return not self.entitled


@dataclass(frozen=True)
class SeatResult:
    granted: bool
    seats: Seats
    reason: str = ""


class EntitlementProvider(ABC):
    """Decides what an organization may do."""

    name = "base"

    @abstractmethod
    def effective(self, organization_id: str) -> Entitlement:
        """The merged capability set for one organization."""

    @abstractmethod
    def claim_seat(self, organization_id: str, user_id: str, instance: str) -> SeatResult:
        """Register a seat/instance at session start."""

    def release_seat(self, organization_id: str, instance: str) -> None:
        """Free a seat. Providers without seat tracking may ignore this."""
        return None
