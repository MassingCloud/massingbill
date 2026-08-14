"""Entitlements from massing.cloud.

An **optional adapter** (SPEC.md 3, 13). The core never imports it; CI deletes
this file and re-runs the whole suite. A standalone install answers "everything,
no limits" from `StandaloneProvider` and never learns this exists.

massing.cloud is a WordPress/WooCommerce storefront, so this talks to
`https://massing.cloud/wp-json` with the same `Authorization: Bearer` header its
own Python SDK uses. There is no Python to import on that side and there never
will be -- the integration is HTTP, which is why the wire conventions in
SPEC.md 3.1 were honoured from P0 rather than retrofitted here.

**A network failure must not stop a contractor billing.** Every call has a
timeout, every failure falls back to the last good answer, and a cache miss on
top of a failure degrades to read-only rather than to locked-out. A pay
application is due on a date somebody else chose; "our licence server was down"
is not a reason a GC can give an owner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from massingbill.services.entitlement.base import (
    UNLIMITED,
    Entitlement,
    EntitlementProvider,
    SeatResult,
    Seats,
)

#: Short. This sits in the request path of a page load.
REQUEST_TIMEOUT = 5.0

#: How long a good answer stays good. Entitlements change when somebody upgrades
#: a plan, which is not something that needs to be noticed within seconds.
CACHE_SECONDS = 300

#: How long a *stale* answer keeps being served once the API is unreachable.
#: Deliberately long: a week of outage should not stop the monthly requisition.
GRACE_SECONDS = 7 * 24 * 60 * 60

#: How long to stop trying after a failure.
#:
#: Without this, every call during an outage pays ``REQUEST_TIMEOUT`` again
#: before returning the same cached answer it already had -- five seconds added
#: to every page load, for as long as the outage lasts. Serving the cached
#: entitlement is the point; paying for the network each time to do it is not.
RETRY_AFTER_FAILURE_SECONDS = 30

#: The capability flags massing's `class-tiers.php` grants. Named here rather
#: than discovered, so a typo on either side is visible instead of silently
#: reading as "not granted".
CAPABILITIES = (
    "gc_billing",
    "billing_projects",
    "billing_apps_per_month",
    "sub_tier_billing",
    "esign",
    "custom_forms",
)


@dataclass
class _Cached:
    entitlement: Entitlement
    fetched_at: float

    def fresh(self, now: float) -> bool:
        return now - self.fetched_at < CACHE_SECONDS

    def within_grace(self, now: float) -> bool:
        return now - self.fetched_at < GRACE_SECONDS


class MassingCloudProvider(EntitlementProvider):
    name = "massing_cloud"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://massing.cloud/wp-json",
        instance: str = "",
    ) -> None:
        if not api_key:
            raise ValueError(
                "The massing.cloud entitlement provider needs an API key "
                "(MASSINGBILL_MASSING_API_KEY)."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.instance = instance
        self._cache: dict[str, _Cached] = {}
        #: When the last fetch failed, per organization. Read before deciding
        #: whether it is worth trying the network again.
        self._failed_at: dict[str, float] = {}

    # ── Reading ─────────────────────────────────────────────────────────────

    def effective(self, organization_id: str) -> Entitlement:
        """The merged capability set, cached, with a long stale-serve window."""
        now = time.monotonic()
        cached = self._cache.get(organization_id)

        if cached is not None and cached.fresh(now):
            return cached.entitlement

        failed_at = self._failed_at.get(organization_id)
        if failed_at is not None and now - failed_at < RETRY_AFTER_FAILURE_SECONDS:
            # Still in the cooldown from a recent failure. Answer from what we
            # have rather than making every caller wait for the same timeout.
            return self._fallback(cached, now)

        try:
            entitlement = self._fetch(organization_id)
        except (requests.RequestException, ValueError, KeyError):
            self._failed_at[organization_id] = now
            return self._fallback(cached, now)

        self._failed_at.pop(organization_id, None)
        self._cache[organization_id] = _Cached(entitlement, now)
        return entitlement

    def _fallback(self, cached: _Cached | None, now: float) -> Entitlement:
        """What to answer when massing.cloud cannot be reached.

        A stale-but-recent answer is served unchanged. Past the grace window,
        or with nothing cached at all, the answer is **read-only rather than
        denied**: data is never destroyed and a customer can still open, read
        and export everything they have. Refusing outright would make an outage
        on our side look like a lapsed subscription on theirs.
        """
        if cached is not None and cached.within_grace(now):
            return cached.entitlement

        return Entitlement(
            tier="unknown",
            entitled=False,
            status="unreachable",
            source=self.name,
        )

    def _fetch(self, organization_id: str) -> Entitlement:
        response = requests.get(
            f"{self.base_url}/massing/v1/entitlements/{organization_id}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return self._parse(dict(response.json()))

    def _parse(self, payload: dict[str, Any]) -> Entitlement:
        """Map massing's entitlement object onto ours.

        The field names already match (SPEC.md 3.1), so this is a read rather
        than a translation -- which is the whole point of having matched them
        while running standalone.
        """
        seats = payload.get("seats") or {}
        limits = dict(payload.get("limits") or {})

        # An absent capability is absent, not permitted. `Entitlement.allows`
        # defaults missing keys to True, which is right for a standalone
        # install and wrong for a metered one.
        for capability in CAPABILITIES:
            limits.setdefault(capability, False)

        return Entitlement(
            tier=str(payload.get("tier", "unknown")),
            entitled=bool(payload.get("entitled", False)),
            status=str(payload.get("status", "active")),
            expires_at=_parse_timestamp(payload.get("expires_at")),
            seats=Seats(
                limit=int(seats.get("limit", UNLIMITED)),
                used=int(seats.get("used", 0)),
            ),
            limits=limits,
            source=self.name,
        )

    # ── Seats ───────────────────────────────────────────────────────────────

    def claim_seat(self, organization_id: str, user_id: str, instance: str) -> SeatResult:
        """Register this instance against the plan's seat count.

        A failure grants the seat. Seat accounting is a billing concern; being
        unable to reach the seat service is our problem, and answering it by
        locking a project manager out of a pay application on the 25th would
        make it theirs.
        """
        try:
            response = requests.post(
                f"{self.base_url}/massing/v1/entitlements/{organization_id}/seats",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                json={"user_id": user_id, "instance": instance or self.instance},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = dict(response.json())
        except (requests.RequestException, ValueError):
            return SeatResult(
                granted=True,
                seats=Seats(),
                reason="massing.cloud was unreachable; the seat was granted locally.",
            )

        seats = payload.get("seats") or {}
        return SeatResult(
            granted=bool(payload.get("granted", True)),
            seats=Seats(
                limit=int(seats.get("limit", UNLIMITED)),
                used=int(seats.get("used", 0)),
            ),
            reason=str(payload.get("reason", "")),
        )

    def release_seat(self, organization_id: str, instance: str) -> None:
        """Best effort. A seat that fails to release expires on their side."""
        try:
            requests.delete(
                f"{self.base_url}/massing/v1/entitlements/{organization_id}/seats",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"instance": instance or self.instance},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return None
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
