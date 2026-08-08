"""The P0 acceptance criteria, as tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill import __version__, create_app
from massingbill.config import Settings, load_settings


def test_boots_with_no_environment_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline P0 criterion: a fresh clone starts with nothing configured.

    Every MASSINGBILL_* variable is cleared and no .env is present, yet the
    factory must produce a working app that picked the standalone defaults.
    """
    monkeypatch.chdir(tmp_path)
    for var in (
        "MASSINGBILL_ENV",
        "MASSINGBILL_SECRET_KEY",
        "MASSINGBILL_DATABASE_URL",
        "MASSINGBILL_ENTITLEMENT_PROVIDER",
        "MASSINGBILL_STORAGE_BACKEND",
        "MASSINGBILL_OIDC_PROVIDERS",
    ):
        monkeypatch.delenv(var, raising=False)

    app = create_app(load_settings(instance_path=tmp_path / "instance"))

    assert app.config["MASSINGBILL_ENTITLEMENT_PROVIDER"] == "standalone"
    assert app.config["MASSINGBILL_STORAGE_BACKEND"] == "local"
    assert app.config["SECRET_KEY"]  # ephemeral key minted for development

    client = app.test_client()
    assert client.get("/healthz").get_json()["status"] == "ok"

    # Readiness too: a fresh clone must reach its own database, not a path
    # Flask-SQLAlchemy re-resolved somewhere else.
    ready = client.get("/readyz")
    assert ready.status_code == 200, ready.get_json()
    assert ready.get_json()["checks"]["database"] == "ok"


def test_the_default_sqlite_url_is_absolute(tmp_path: Path) -> None:
    """Regression: a relative SQLite URL is re-resolved against Flask's own
    instance folder, which points at a directory that does not exist -- the
    application boots but every database read fails.
    """
    settings = Settings(env="testing", secret_key="x" * 32, instance_path=tmp_path / "inst")

    assert settings.database_url.startswith("sqlite:///")
    assert settings.instance_path.is_absolute()
    assert Path(settings.database_url.removeprefix("sqlite:///")).is_absolute()


def test_healthz_reports_version(client: FlaskClient) -> None:
    body = client.get("/healthz").get_json()
    assert body == {"status": "ok", "service": "massingbill", "version": __version__}


def test_readyz_checks_the_database(client: FlaskClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["entitlement_provider"] == "standalone"


def test_index_renders(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert b"Massing" in response.data


def test_index_carries_the_aia_disclaimer(client: FlaskClient) -> None:
    """SPEC.md 0.2: the trademark disclaimer is not optional chrome."""
    # Normalise whitespace: the template wraps, but the sentence must be intact.
    body = " ".join(client.get("/").get_data(as_text=True).split())
    assert "not affiliated with, endorsed by, or sponsored by" in body
    assert "The American Institute of Architects" in body


def test_unknown_route_renders_an_html_error(client: FlaskClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert b"404" in response.data


def test_unknown_api_route_returns_json(client: FlaskClient) -> None:
    response = client.get("/api/massingbill/v1/nope")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not_found"


def test_request_id_is_echoed(client: FlaskClient) -> None:
    response = client.get("/healthz", headers={"X-Request-Id": "abc123"})
    assert response.headers["X-Request-Id"] == "abc123"


def test_request_id_is_generated_when_absent(client: FlaskClient) -> None:
    assert client.get("/healthz").headers["X-Request-Id"]


def test_production_refuses_to_start_without_a_secret_key() -> None:
    """A rotating key silently invalidates every session; refuse instead."""
    with pytest.raises(ValueError, match="MASSINGBILL_SECRET_KEY is required"):
        Settings(env="production", secret_key="")


def test_development_relaxes_the_secure_cookie_flag() -> None:
    settings = Settings(env="development", secret_key="x" * 32)
    assert settings.session_cookie_secure is False
    assert settings.preferred_url_scheme == "http"


def test_app_context_exposes_the_resolved_adapters(app: Flask) -> None:
    assert app.extensions["massingbill_entitlement"].name == "standalone"
    assert app.extensions["massingbill_storage"].name == "local"
