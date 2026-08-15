"""Enforcing what an entitlement actually allows.

``services/entitlement`` decides *what* a customer may do. This module is where
the rest of the application *asks*, and it is deliberately the only place that
does -- so "where are we gated?" is a grep for ``limits.require`` rather than an
archaeology exercise.

Three things worth stating, because each is a real choice:

**A standalone install is unaffected.** ``StandaloneProvider`` returns no limits
at all, and both :meth:`Entitlement.allows` and :meth:`Entitlement.within`
default an absent key to *permitted*. Every call below is therefore a no-op
unless an operator has opted into a provider that says otherwise. There is no
licence, no phone-home and no kill switch (SPEC.md 3.3); this changes none of
that.

**The provider is consulted once per organization per request**, cached on
``g``. The massing.cloud adapter caches on its own clock as well, including
negatively -- see ``RETRY_AFTER_FAILURE_SECONDS`` there. Without the local cache
a single page that creates two things would ask twice for an answer that cannot
have changed in between.

**Gating is at the write points, not in a ``before_request`` hook.** A blanket
"refuse every POST" would be less code and would catch more, but it would also
refuse things a lapsed customer must still be able to do -- signing out, paying,
exporting their own data. Naming the operations makes the refusals reviewable,
which matters when the refusal is somebody's month-end.
"""

from __future__ import annotations

from flask import current_app, g

from massingbill.errors import EntitlementError
from massingbill.services.entitlement import Entitlement, EntitlementProvider

#: Capability flags, matching massing.cloud's tier catalog verbatim. A typo here
#: reads as "granted" rather than as an error -- absent keys default to allowed,
#: which is right for standalone and silent everywhere else -- so they are named
#: constants and every call site uses one.
GC_BILLING = "gc_billing"
SUB_TIER_BILLING = "sub_tier_billing"
ESIGN = "esign"
CUSTOM_FORMS = "custom_forms"

#: Counted limits.
BILLING_PROJECTS = "billing_projects"
BILLING_APPS_PER_MONTH = "billing_apps_per_month"

_CACHE_KEY = "_massingbill_entitlements"


def provider() -> EntitlementProvider:
    """The configured provider. Resolved once at boot in :func:`create_app`."""
    resolved: EntitlementProvider = current_app.extensions["massingbill_entitlement"]
    return resolved


def effective(organization_id: str) -> Entitlement:
    """This organization's entitlement, once per request.

    Cached on ``g`` rather than memoised on the provider, so the cache dies with
    the request and a customer who upgrades mid-session sees it on their next
    click rather than after a restart.
    """
    cache: dict[str, Entitlement] = g.setdefault(_CACHE_KEY, {})
    if organization_id not in cache:
        cache[organization_id] = provider().effective(organization_id)
    return cache[organization_id]


def require(capability: str, organization_id: str, *, what: str) -> None:
    """Refuse unless a boolean capability is granted.

    ``what`` completes the sentence "Your plan does not include ...", so it
    reads as a noun phrase: ``"subcontractor billing"``, not ``"create a sub"``.
    """
    entitlement = effective(organization_id)
    if entitlement.allows(capability):
        return

    raise EntitlementError(
        f"Your plan does not include {what}."
        + (
            " Your subscription has lapsed, so this deployment is read-only."
            if entitlement.read_only
            else ""
        ),
        details={"capability": capability, "tier": entitlement.tier},
    )


def require_within(name: str, current: int, organization_id: str, *, what: str) -> None:
    """Refuse when a counted resource has reached its cap.

    ``current`` is the count *before* the thing being created, so the caller
    counts what exists and this decides whether one more is allowed.
    """
    entitlement = effective(organization_id)

    # A lapsed subscription is refused here too, not only by whichever
    # capability gate happens to run first. `Entitlement.within` answers a
    # question about counts and knows nothing about `entitled`, so relying on
    # call order would mean a future counted gate added without a capability
    # gate beside it silently let a lapsed customer through.
    if entitlement.read_only:
        raise EntitlementError(
            "Your subscription has lapsed, so this deployment is read-only.",
            details={"limit": name, "tier": entitlement.tier},
        )

    if entitlement.within(name, current):
        return

    cap = entitlement.limit(name)
    raise EntitlementError(
        f"Your plan allows {cap} {what}, and you have {current}.",
        details={"limit": name, "cap": cap, "current": current, "tier": entitlement.tier},
    )
