"""CLI, error rendering, logging and probe-failure behaviour."""

from __future__ import annotations

import json
import logging

import pytest
from flask import Flask
from flask.testing import FlaskClient

from massingbill import __version__
from massingbill.cli import main as cli_main
from massingbill.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from massingbill.logging_config import JsonFormatter

# ── CLI ─────────────────────────────────────────────────────────────────────


def test_gen_secret_prints_a_usable_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["gen-secret"]) == 0
    assert len(capsys.readouterr().out.strip()) >= 43


def test_gen_secret_is_not_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    cli_main(["gen-secret"])
    first = capsys.readouterr().out
    cli_main(["gen-secret"])
    assert capsys.readouterr().out != first


def test_check_reports_the_resolved_configuration(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    assert cli_main(["check"]) == 0

    out = capsys.readouterr().out
    assert f"massingbill {__version__}" in out
    assert "entitlement provider standalone" in out
    assert "storage backend      local" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


# ── Error rendering ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (ValidationError("bad input"), 400, "validation_error"),
        (ForbiddenError("not yours"), 403, "forbidden"),
        (NotFoundError("no such project"), 404, "not_found"),
        (ConflictError("period already closed"), 409, "conflict"),
    ],
)
def test_typed_errors_render_as_json_on_the_api(
    app: Flask, client: FlaskClient, exception: Exception, status: int, code: str
) -> None:
    @app.get("/api/massingbill/v1/_boom")
    def boom() -> str:
        raise exception

    response = client.get("/api/massingbill/v1/_boom")
    assert response.status_code == status
    assert response.get_json() == {"error": code, "message": str(exception)}


def test_typed_errors_render_as_html_in_the_browser(app: Flask, client: FlaskClient) -> None:
    @app.get("/_boom")
    def boom() -> str:
        raise NotFoundError("no such project")

    response = client.get("/_boom")
    assert response.status_code == 404
    assert b"no such project" in response.data


def test_error_details_are_included_when_present(app: Flask, client: FlaskClient) -> None:
    @app.get("/api/massingbill/v1/_detail")
    def detail() -> str:
        raise ValidationError("line out of balance", details={"line": "5a", "delta_cents": 1})

    body = client.get("/api/massingbill/v1/_detail").get_json()
    assert body["details"] == {"line": "5a", "delta_cents": 1}


def test_unexpected_exceptions_do_not_leak_internals(app: Flask, client: FlaskClient) -> None:
    @app.get("/api/massingbill/v1/_explode")
    def explode() -> str:
        raise RuntimeError("connection string: postgres://user:hunter2@db/prod")

    response = client.get("/api/massingbill/v1/_explode")
    assert response.status_code == 500
    body = response.get_json()
    assert body == {"error": "internal_error", "message": "An unexpected error occurred."}
    assert "hunter2" not in response.get_data(as_text=True)


# ── Logging ─────────────────────────────────────────────────────────────────


def test_json_formatter_emits_parseable_records() -> None:
    record = logging.LogRecord(
        name="massingbill",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="period %s closed",
        args=("2026-07",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "period 2026-07 closed"
    assert payload["logger"] == "massingbill"


def test_json_formatter_includes_the_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="massingbill",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


# ── Readiness ───────────────────────────────────────────────────────────────


def test_readyz_reports_not_ready_when_the_database_is_gone(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database blip must surface as 503 on /readyz, never as a 500."""
    from massingbill.extensions import db

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(db.session, "execute", explode)

    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error"


def test_healthz_does_not_touch_the_database(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must stay green through a database outage, or the container gets killed."""
    from massingbill.extensions import db

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("/healthz must not query the database")

    monkeypatch.setattr(db.session, "execute", explode)
    assert client.get("/healthz").status_code == 200
