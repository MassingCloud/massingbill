"""Documents in the massing.cloud vault.

An **optional adapter** (SPEC.md 3, 13). The core never imports it; CI deletes
this file and re-runs the suite. `LocalStorage` is the default and a standalone
install never learns this exists.

Like every backend here it returns opaque pointers and never a public URL. The
vault issues signed, expiring links; handing one to a caller would turn a
pointer into a bearer token for a financial document, sitting in whatever proxy
log it passed through. Downloads stay mediated by our own ownership check.

**Writes are verified, not assumed.** The digest is computed from the bytes we
sent and checked again on the way back, because a document store is durable but
not immutable, and a signed lien waiver whose bytes changed is precisely the
case worth catching.
"""

from __future__ import annotations

import hashlib
import io
from typing import BinaryIO

import requests

from massingbill.services.storage.base import StorageBackend, StoragePointer

REQUEST_TIMEOUT = 30.0


class MassingVaultStorage(StorageBackend):
    name = "massing_vault"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://massing.cloud/wp-json",
        prefix: str = "massingbill",
    ) -> None:
        if not api_key:
            raise ValueError(
                "The Massing Vault storage backend needs an API key (MASSINGBILL_MASSING_API_KEY)."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix.strip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _url(self, *parts: str) -> str:
        return "/".join([self.base_url, "massing-vault/v1", *parts])

    def put(self, key: str, stream: BinaryIO, *, content_type: str) -> StoragePointer:
        payload = stream.read()
        digest = hashlib.sha256(payload).hexdigest()
        full_key = self._full_key(key)

        response = requests.put(
            self._url("objects", full_key),
            headers={**self._headers, "Content-Type": content_type, "X-Content-SHA256": digest},
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        # If the vault reports a digest, it must be the one we sent. A store
        # that silently transformed the bytes -- re-encoding, stripping, adding
        # a header -- has stored a different document from the one that was
        # signed, and only the digest would ever say so.
        reported = str(dict(response.json() or {}).get("sha256", "")) or digest
        if reported != digest:
            raise ValueError(
                f"The vault stored {full_key} with a different digest than was sent. "
                "The bytes were altered in transit or on write."
            )

        return StoragePointer(
            backend=self.name,
            key=key,
            size=len(payload),
            sha256=digest,
            content_type=content_type,
        )

    def open(self, pointer: StoragePointer) -> BinaryIO:
        response = requests.get(
            self._url("objects", self._full_key(pointer.key)),
            headers=self._headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.content

        actual = hashlib.sha256(payload).hexdigest()
        if pointer.sha256 and actual != pointer.sha256:
            raise ValueError(
                f"{pointer.key} no longer matches the digest recorded when it was stored. "
                "It has been modified or replaced since."
            )

        return io.BytesIO(payload)

    def delete(self, pointer: StoragePointer) -> None:
        response = requests.delete(
            self._url("objects", self._full_key(pointer.key)),
            headers=self._headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code not in (200, 202, 204, 404):
            response.raise_for_status()

    def exists(self, pointer: StoragePointer) -> bool:
        response = requests.head(
            self._url("objects", self._full_key(pointer.key)),
            headers=self._headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
