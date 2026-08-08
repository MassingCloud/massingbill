"""The storage seam.

Rendered pay-app packages, waivers and compliance uploads are written through
this interface. ``LocalStorage`` is the default and keeps everything on a
protected filesystem path; S3 and the massing vault are optional adapters.

Storage never returns a public URL. Callers get an opaque
:class:`StoragePointer`, and downloads are always mediated by an ownership check
plus a signed, expiring token -- so a leaked path is not a leaked document.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True)
class StoragePointer:
    """Where an object lives, in terms only its own backend understands."""

    backend: str
    key: str
    size: int
    sha256: str
    content_type: str = "application/octet-stream"


class StorageBackend(ABC):
    name = "base"

    @abstractmethod
    def put(self, key: str, stream: BinaryIO, *, content_type: str) -> StoragePointer:
        """Store bytes under ``key`` and return a pointer to them."""

    @abstractmethod
    def open(self, pointer: StoragePointer) -> BinaryIO:
        """Open the stored object for reading."""

    @abstractmethod
    def delete(self, pointer: StoragePointer) -> None:
        """Remove the stored object."""

    @abstractmethod
    def exists(self, pointer: StoragePointer) -> bool:
        """True when the object is still present."""
