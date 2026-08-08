"""Liveness and readiness probes.

``/healthz`` answers "is the process up" and must never touch the database, so a
database blip does not take the container out of rotation. ``/readyz`` answers
"can it serve traffic" and does check the database.

Both are exempt from rate limiting and from CSRF, and neither reveals anything a
prober should not see.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from massingbill.extensions import db, limiter

bp = Blueprint("health", __name__)


@bp.get("/healthz")
@limiter.exempt
def healthz() -> Any:
    return jsonify(
        {
            "status": "ok",
            "service": "massingbill",
            "version": current_app.config["MASSINGBILL_VERSION"],
        }
    )


@bp.get("/readyz")
@limiter.exempt
def readyz() -> Any:
    checks: dict[str, str] = {}
    ready = True

    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the probe must report, not raise
        current_app.logger.warning("readiness check failed: %s", exc)
        checks["database"] = "error"
        ready = False

    body = {
        "status": "ready" if ready else "not_ready",
        "service": "massingbill",
        "version": current_app.config["MASSINGBILL_VERSION"],
        "checks": checks,
        # Named so an operator can confirm at a glance that a deployment is
        # running standalone rather than reaching for a cloud it does not need.
        "entitlement_provider": current_app.config["MASSINGBILL_ENTITLEMENT_PROVIDER"],
        "storage_backend": current_app.config["MASSINGBILL_STORAGE_BACKEND"],
    }
    return jsonify(body), (200 if ready else 503)
