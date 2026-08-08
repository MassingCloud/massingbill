"""Storage adapters. Optional backends are imported lazily; see the entitlement package."""

from __future__ import annotations

from typing import Any

from massingbill.errors import AdapterUnavailableError

from .base import StorageBackend, StoragePointer
from .local import LocalStorage

__all__ = ["LocalStorage", "StorageBackend", "StoragePointer", "get_backend"]


def get_backend(name: str, **options: Any) -> StorageBackend:
    if name == "local":
        return LocalStorage(**options)
    if name == "s3":
        try:
            from .s3 import S3Storage
        except ImportError as exc:  # pragma: no cover - exercised in adapter tests
            raise AdapterUnavailableError(
                "The S3 storage adapter is not installed. "
                "Install it with: pip install 'massingbill[s3]'"
            ) from exc
        return S3Storage(**options)
    if name == "massing_vault":
        try:
            from .massing_vault import MassingVaultStorage
        except ImportError as exc:  # pragma: no cover - exercised in adapter tests
            raise AdapterUnavailableError(
                "The Massing Vault storage adapter is not installed. "
                "Install it with: pip install 'massingbill[oidc]'"
            ) from exc
        return MassingVaultStorage(**options)
    raise ValueError(f"Unknown storage backend: {name!r}")
