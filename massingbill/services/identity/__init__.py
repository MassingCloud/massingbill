"""Identity adapters. OIDC is imported lazily and only when configured."""

from __future__ import annotations

from typing import Any

from massingbill.errors import AdapterUnavailableError

from .base import IdentityClaim, IdentityProvider
from .local import LocalCredential, LocalPasswordProvider

__all__ = [
    "IdentityClaim",
    "IdentityProvider",
    "LocalCredential",
    "LocalPasswordProvider",
    "get_provider",
]


def get_provider(name: str, **options: Any) -> IdentityProvider:
    if name == "local":
        return LocalPasswordProvider(**options)
    try:
        from .oidc import OidcProvider
    except ImportError as exc:  # pragma: no cover - exercised in adapter tests
        raise AdapterUnavailableError(
            "OIDC sign-in is not installed. Install it with: pip install 'massingbill[oidc]'"
        ) from exc
    return OidcProvider(name=name, **options)
