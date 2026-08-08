"""The default storage backend: a protected local directory.

Keys are namespaced but never trusted -- every key is normalised and checked to
land inside the root, so a crafted key cannot traverse out of it. Files are
written to a temporary name and moved into place, so a crash never leaves a
half-written document that later renders as a corrupt pay app.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from massingbill.errors import NotFoundError, ValidationError

from .base import StorageBackend, StoragePointer

_CHUNK = 1024 * 1024


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key:
            raise ValidationError(f"Invalid storage key: {key!r}")
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValidationError(f"Storage key escapes the storage root: {key!r}")
        return candidate

    def put(self, key: str, stream: BinaryIO, *, content_type: str) -> StoragePointer:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    tmp.write(chunk)
            shutil.move(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        return StoragePointer(
            backend=self.name,
            key=key,
            size=size,
            sha256=digest.hexdigest(),
            content_type=content_type,
        )

    def open(self, pointer: StoragePointer) -> BinaryIO:
        path = self._resolve(pointer.key)
        if not path.is_file():
            raise NotFoundError(f"Stored object is missing: {pointer.key}")
        return path.open("rb")

    def delete(self, pointer: StoragePointer) -> None:
        self._resolve(pointer.key).unlink(missing_ok=True)

    def exists(self, pointer: StoragePointer) -> bool:
        return self._resolve(pointer.key).is_file()
