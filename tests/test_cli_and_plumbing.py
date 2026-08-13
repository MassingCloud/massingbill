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


# ── The statutory worksheet, end to end through the CLI ─────────────────────
#
# Driven through `main()` with a real database, because the point of these
# commands is that somebody who is not a developer can run them. A service test
# would not catch a broken argument parser or a command that never commits.


def statutory_org(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real organization on a real on-disk database, seeded and committed."""
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("MASSINGBILL_DATABASE_URL", f"sqlite:///{tmp_path}/mb.sqlite")  # type: ignore[str-bytes-safe]

    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.services import accounts, deadlines
    from massingbill.services import waivers as waiver_service

    app = create_app()
    with app.app_context():
        db.create_all()
        user = accounts.create_user("owner@acme.example", "correct-horse-battery-staple")
        organization = accounts.create_organization("Acme Construction", user)
        waiver_service.seed_templates(organization)
        deadlines.seed_rules(organization)
        db.session.commit()
        return organization.id


def test_statutory_status_reports_what_is_outstanding(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = statutory_org(tmp_path, monkeypatch)

    assert cli_main(["statutory", "status", "--organization", org]) == 0

    out = capsys.readouterr().out
    assert "unverified" in out
    assert "ship empty on purpose" in out


def test_statutory_export_writes_a_worksheet(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = statutory_org(tmp_path, monkeypatch)
    out_file = f"{tmp_path}/statutory.csv"

    assert cli_main(["statutory", "export", "--organization", org, "--out", out_file]) == 0

    from pathlib import Path

    content = Path(out_file).read_text(encoding="utf-8")
    assert "verbatim_text" in content
    assert "Enter the exact wording" in content
    assert "Fill the `verbatim_text` column" in capsys.readouterr().out


def test_statutory_export_can_be_narrowed_to_a_state(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = statutory_org(tmp_path, monkeypatch)

    assert cli_main(["statutory", "export", "--organization", org, "--state", "CA"]) == 0
    out = capsys.readouterr().out
    assert ",CA," in out
    assert ",TX," not in out


def test_statutory_import_verifies_and_persists(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end reason the commands exist: refusals stop, and it sticks
    across a fresh application context."""
    import csv
    import io
    from pathlib import Path

    org = statutory_org(tmp_path, monkeypatch)
    sheet = f"{tmp_path}/statutory.csv"
    cli_main(["statutory", "export", "--organization", org, "--out", sheet])
    capsys.readouterr()

    from massingbill.services import statutory

    rows = list(csv.DictReader(io.StringIO(Path(sheet).read_text(encoding="utf-8").lstrip("﻿"))))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=statutory.COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        if row["kind"] == "waiver":
            row["verbatim_text"] = "THE VERBATIM STATUTORY TEXT AS ENACTED " * 3
        else:
            row["days"] = "90"
            row["citation"] = row["citation"] or "the section that was read"
        writer.writerow(row)
    Path(sheet).write_text(buffer.getvalue(), encoding="utf-8")

    assert cli_main(["statutory", "import", sheet, "--organization", org]) == 0
    assert "verified" in capsys.readouterr().out

    assert cli_main(["statutory", "status", "--organization", org]) == 0
    assert "0 lien-waiver form(s) and 0 deadline rule(s) unverified" in capsys.readouterr().out


def test_statutory_import_of_a_missing_file_is_a_clean_error(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = statutory_org(tmp_path, monkeypatch)

    assert cli_main(["statutory", "import", f"{tmp_path}/nope.csv", "--organization", org]) == 1
    assert "No such file" in capsys.readouterr().err


def test_statutory_with_no_subcommand_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["statutory"]) == 1


def test_handoff_prune_deletes_only_stale_records(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prune()` existed with no caller for one commit, which meant the table
    grew by one row per sign-in forever. This is the caller."""
    from datetime import timedelta

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("MASSINGBILL_DATABASE_URL", f"sqlite:///{tmp_path}/mb.sqlite")  # type: ignore[str-bytes-safe]

    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.models import SpentHandoff
    from massingbill.models.base import utcnow

    app = create_app()
    with app.app_context():
        db.create_all()
        db.session.add(SpentHandoff(jti="stale", used_at=utcnow() - timedelta(days=1)))
        db.session.add(SpentHandoff(jti="recent", used_at=utcnow()))
        db.session.commit()

    assert cli_main(["handoff", "prune"]) == 0
    assert "Pruned 1" in capsys.readouterr().out

    with app.app_context():
        remaining = [row.jti for row in db.session.query(SpentHandoff).all()]

    assert remaining == ["recent"], "a live record was deleted"


def test_handoff_with_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["handoff"]) == 1
