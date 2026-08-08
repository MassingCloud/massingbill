"""Password hashing, signed tokens and response hardening.

The CSP is strict ``default-src 'self'`` with no inline script and no external
host. Everything the UI needs must therefore be served from this application --
which is why the TOTP enrolment QR is rendered as an inline SVG by segno rather
than fetched from a chart service (SPEC.md 9).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from flask import Flask, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Tuned for an interactive login on modest hardware: ~50-100 ms per verify.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


def generate_token(nbytes: int = 32) -> str:
    """A URL-safe random token for invitations, share links and API keys."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Store only the digest, so a database leak does not yield usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_payload(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 -- the scheme every outbound webhook uses (SPEC.md 3.1)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(secret, body), signature)


def make_serializer(app: Flask, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=salt)


def load_timed_token(app: Flask, salt: str, token: str, max_age: int) -> Any | None:
    """Return the payload, or ``None`` if the token is bad or expired."""
    try:
        return make_serializer(app, salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def _apply(response: Response) -> Response:
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
