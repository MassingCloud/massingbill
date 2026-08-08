"""Structured logging.

JSON by default so logs are queryable in production; a request id is attached
to every record so a support ticket can be traced end to end. Nothing here ever
logs a secret -- values from ``Settings`` are not serialised into log records.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

from flask import Flask, g, has_request_context, request

REQUEST_ID_HEADER = "X-Request-Id"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if has_request_context():
            payload["request_id"] = getattr(g, "request_id", None)
            payload["method"] = request.method
            payload["path"] = request.path
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(app: Flask, *, level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if as_json
        else logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    app.logger.handlers = [handler]
    app.logger.setLevel(level.upper())
    app.logger.propagate = False

    @app.before_request
    def _assign_request_id() -> None:
        g.request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex

    @app.after_request
    def _echo_request_id(response: Any) -> Any:
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers.setdefault(REQUEST_ID_HEADER, request_id)
        return response
