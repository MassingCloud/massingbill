"""HTTP layer. Blueprints never import adapters directly -- they go through services."""

from __future__ import annotations

from flask import Flask

from . import health, main


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(health.bp)
    app.register_blueprint(main.bp)
