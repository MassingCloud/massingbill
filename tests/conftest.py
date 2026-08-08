"""Test fixtures.

Two rules the whole suite depends on:

1. Settings are constructed explicitly, never read from the ambient
   environment, so a developer's ``.env`` cannot change a test result.
2. The network is blocked at the socket layer (see ``block_network``), so an
   accidental outbound call is a loud failure rather than a slow test. This is
   the mechanical half of the standalone promise in SPEC.md section 3.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill import create_app
from massingbill.config import Settings
from massingbill.extensions import db


class NetworkBlockedError(RuntimeError):
    """Raised when test code attempts an outbound connection."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any outbound socket connection.

    Loopback stays open so a live-server fixture would still work; anything
    else means the core reached for a network it is not allowed to need.
    """
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(self, address, *args, **kwargs)  # type: ignore[arg-type]
        raise NetworkBlockedError(
            f"Outbound network access is blocked in tests (attempted {host!r}). "
            "The standalone core must not require the network."
        )

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="testing",
        secret_key="test-secret-key-not-for-production",
        instance_path=tmp_path / "instance",
        database_url="sqlite:///:memory:",
        log_json=False,
        log_level="WARNING",
        session_cookie_secure=False,
        rate_limit_default="1000 per hour",
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[Flask]:
    application = create_app(settings)
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()
