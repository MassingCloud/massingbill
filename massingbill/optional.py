"""Which modules are optional, and why.

Single-sourced because three places need to agree about it: the
standalone-integrity test, the ``offline`` CI job, and anyone reasoning about
what a minimal deployment actually installs. When they drifted, the CI job
walked a module needing an extra it had not installed and failed on a fact the
test already knew.

Two different claims, kept apart deliberately:

**Adapters** are couplings we refuse. The core must never import one, and CI
deletes them and re-runs the suite to prove it (SPEC.md 3, 13).

**Extras** are dependencies we make optional. ``massingbill[render]`` pulls in
WeasyPrint and openpyxl; a headless API deployment installs neither and must
still boot, offer the formats it *can* produce, and say what is missing.
"""

from __future__ import annotations

#: Optional integration adapters. Lazily imported, never referenced by the core.
ADAPTER_MODULES = frozenset(
    {
        "massingbill.services.entitlement.massing_cloud",
        "massingbill.services.identity.oidc",
        "massingbill.services.storage.s3",
        "massingbill.services.storage.massing_vault",
    }
)

#: Modules that need an optional extra rather than an optional adapter.
EXTRA_MODULES = frozenset(
    {
        "massingbill.services.renderers.pdf",
        "massingbill.services.renderers.xlsx",
    }
)

#: Everything a minimal install may legitimately be unable to import.
OPTIONAL_MODULES = ADAPTER_MODULES | EXTRA_MODULES

#: Package prefix for third-party integrations (Procore, QBO, Sage). Populated
#: in P7; never imported by the core.
INTEGRATIONS_PREFIX = "massingbill.services.integrations"


def is_optional(module_name: str) -> bool:
    """True when a minimal install is allowed not to have this module."""
    return module_name in OPTIONAL_MODULES or module_name.startswith(INTEGRATIONS_PREFIX)
