"""Security posture tests (SPEC.md 9)."""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill.security import (
    CONTENT_SECURITY_POLICY,
    generate_token,
    hash_password,
    hash_token,
    sign_payload,
    verify_password,
    verify_signature,
)


def test_security_headers_are_present_on_every_response(client: FlaskClient) -> None:
    headers = client.get("/").headers
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_csp_is_strict_and_allows_no_external_host() -> None:
    """The whole UI must be self-hosted; an inline script would break this."""
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "https://" not in CONTENT_SECURITY_POLICY


def test_hsts_only_when_cookies_are_secure(app: Flask, client: FlaskClient) -> None:
    assert "Strict-Transport-Security" not in client.get("/").headers

    app.config["SESSION_COOKIE_SECURE"] = True
    assert "Strict-Transport-Security" in client.get("/").headers


def test_password_round_trip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "correct horse battery staple") is True
    assert verify_password(stored, "wrong password") is False


def test_password_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_verify_password_tolerates_a_corrupt_hash() -> None:
    assert verify_password("not-a-hash", "anything") is False


def test_tokens_are_stored_only_as_digests() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert digest != token
    assert len(digest) == 64
    assert hash_token(token) == digest


def test_webhook_signature_round_trip() -> None:
    """Hex HMAC-SHA256, wire-compatible with the massing convention."""
    body = b'{"event":"application.submitted"}'
    signature = sign_payload("shared-secret", body)

    assert len(signature) == 64
    assert verify_signature("shared-secret", body, signature) is True
    assert verify_signature("wrong-secret", body, signature) is False
    assert verify_signature("shared-secret", b'{"event":"tampered"}', signature) is False


@pytest.mark.parametrize("path", ["/", "/healthz", "/readyz"])
def test_no_server_banner_leaks_internals(client: FlaskClient, path: str) -> None:
    assert "X-Powered-By" not in client.get(path).headers
