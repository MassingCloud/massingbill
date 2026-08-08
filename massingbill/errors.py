"""Typed application errors and their HTTP/JSON rendering.

The error envelope deliberately matches the massing convention (SPEC.md 3.1):
``401`` bad secret, ``404`` not found, ``409`` limit reached, ``429`` rate
limited -- so a client written against one API reads the other without
surprise. Honoring the shape costs nothing and is expensive to retrofit.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException


class MassingBillError(Exception):
    """Base class for errors this application raises deliberately."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(MassingBillError):
    status_code = 400
    code = "validation_error"


class AuthenticationError(MassingBillError):
    status_code = 401
    code = "unauthenticated"


class ForbiddenError(MassingBillError):
    """Authenticated, but not allowed. Named to avoid shadowing the builtin."""

    status_code = 403
    code = "forbidden"


class NotFoundError(MassingBillError):
    status_code = 404
    code = "not_found"


class ConflictError(MassingBillError):
    """A limit or a state machine refused the operation (mirrors massing's 409)."""

    status_code = 409
    code = "conflict"


class EntitlementError(ConflictError):
    """A configured entitlement provider declined. Never raised in standalone mode."""

    code = "entitlement_denied"


class AdapterUnavailableError(MassingBillError):
    """An optional adapter was selected but its dependencies are not installed."""

    status_code = 503
    code = "adapter_unavailable"


def wants_json() -> bool:
    """True when the caller is an API client rather than a browser."""
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    return accept.best == "application/json" or (
        accept["application/json"] >= accept["text/html"] and accept["application/json"] > 0
    )


def register_error_handlers(app: Flask) -> None:
    """Attach handlers that render both JSON and HTML from one code path."""

    @app.errorhandler(MassingBillError)
    def _handle_app_error(exc: MassingBillError) -> tuple[Response | str, int]:
        if wants_json():
            return jsonify(exc.to_dict()), exc.status_code
        return render_template("error.html", code=exc.status_code, message=exc.message), (
            exc.status_code
        )

    @app.errorhandler(HTTPException)
    def _handle_http_error(exc: HTTPException) -> tuple[Response | str, int]:
        status = exc.code or 500
        message = exc.description or ""
        if wants_json():
            code = (exc.name or "error").lower().replace(" ", "_")
            return jsonify({"error": code, "message": message}), status
        return render_template("error.html", code=status, message=message), status

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception) -> tuple[Response | str, int]:
        # Log with the stack trace, but never leak internals to the caller.
        app.logger.exception("unhandled exception", exc_info=exc)
        message = "An unexpected error occurred."
        if wants_json():
            return jsonify({"error": "internal_error", "message": message}), 500
        return render_template("error.html", code=500, message=message), 500
