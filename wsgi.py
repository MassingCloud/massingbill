"""WSGI entry point for gunicorn: ``gunicorn -c deploy/gunicorn.conf.py wsgi:app``."""

from __future__ import annotations

from massingbill import create_app

app = create_app()
