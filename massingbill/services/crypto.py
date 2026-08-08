"""Authenticated encryption for secrets held at rest.

Used for TOTP seeds now and integration tokens later. AES-256-GCM with a random
96-bit nonce; the ciphertext carries a version prefix so the scheme can be
rotated without guessing at what an old row contains.

**Key management, stated plainly because it is an operational hazard.** The key
comes from ``MASSINGBILL_ENCRYPTION_KEY``. If that is unset outside production
one is derived from ``SECRET_KEY`` so a developer never has to configure two
things to log in -- but that also means **rotating ``SECRET_KEY`` in such a
deployment makes every stored TOTP seed undecryptable**, locking out every user
with two-factor enabled. Production therefore requires an explicit, separately
rotatable key, and refuses to start without one.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VERSION = "v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_INFO = b"massingbill/secret-box/v1"


class DecryptionError(ValueError):
    """A stored secret could not be decrypted -- wrong key, or tampering."""


def derive_key(material: str, *, salt: bytes = b"massingbill") -> bytes:
    """Stretch arbitrary key material into a 256-bit key."""
    if not material:
        raise ValueError("Encryption key material must not be empty")
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=salt, info=_INFO).derive(
        material.encode("utf-8")
    )


class SecretBox:
    """Encrypts and decrypts short strings with a single symmetric key."""

    def __init__(self, key_material: str) -> None:
        self._aead = AESGCM(derive_key(key_material))

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return f"{_VERSION}:{base64.urlsafe_b64encode(nonce + sealed).decode('ascii')}"

    def decrypt(self, token: str) -> str:
        version, _, payload = token.partition(":")
        if version != _VERSION or not payload:
            raise DecryptionError(f"Unrecognised ciphertext format: {token[:16]!r}...")

        try:
            raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise DecryptionError("Ciphertext is not valid base64") from exc

        if len(raw) <= _NONCE_BYTES:
            raise DecryptionError("Ciphertext is truncated")

        try:
            plaintext = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)
        except InvalidTag as exc:
            raise DecryptionError(
                "Could not decrypt: the key is wrong or the value was tampered with"
            ) from exc

        return plaintext.decode("utf-8")
