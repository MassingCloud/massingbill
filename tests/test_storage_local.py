"""Local storage backend tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from massingbill.errors import NotFoundError, ValidationError
from massingbill.services.storage import LocalStorage, StoragePointer


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def test_put_returns_size_and_digest(storage: LocalStorage) -> None:
    pointer = storage.put("org/app-1.pdf", io.BytesIO(b"hello"), content_type="application/pdf")

    assert pointer.backend == "local"
    assert pointer.size == 5
    assert pointer.sha256 == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
    assert storage.exists(pointer) is True


def test_round_trip(storage: LocalStorage) -> None:
    pointer = storage.put("a/b/c.txt", io.BytesIO(b"payload"), content_type="text/plain")
    with storage.open(pointer) as handle:
        assert handle.read() == b"payload"


def test_delete_is_idempotent(storage: LocalStorage) -> None:
    pointer = storage.put("gone.txt", io.BytesIO(b"x"), content_type="text/plain")
    storage.delete(pointer)
    storage.delete(pointer)
    assert storage.exists(pointer) is False


def test_opening_a_missing_object_raises_not_found(storage: LocalStorage) -> None:
    pointer = StoragePointer(backend="local", key="never-written.pdf", size=0, sha256="")
    with pytest.raises(NotFoundError):
        storage.open(pointer)


@pytest.mark.parametrize("key", ["../escape.txt", "/absolute.txt", "a/../../escape.txt", ""])
def test_keys_cannot_escape_the_storage_root(storage: LocalStorage, key: str) -> None:
    with pytest.raises(ValidationError):
        storage.put(key, io.BytesIO(b"x"), content_type="text/plain")


def test_a_failed_write_leaves_no_partial_file(storage: LocalStorage, tmp_path: Path) -> None:
    """A crash mid-write must not leave a truncated document that later renders."""

    class Exploding(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise OSError("disk went away")

    with pytest.raises(OSError, match="disk went away"):
        storage.put("doomed.pdf", Exploding(b"x"), content_type="application/pdf")

    leftovers = list((tmp_path / "storage").rglob("*"))
    assert leftovers == [], f"partial files left behind: {leftovers}"
