"""HTTP layer. Blueprints never import adapters directly -- they go through services."""

from __future__ import annotations

from flask import Flask

from . import auth, health, main, projects, sov


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(health.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(sov.bp)


def register_template_helpers(app: Flask) -> None:
    """Filters and globals the templates rely on.

    ``money`` and ``pct`` route every rendered amount through the money kernel,
    so a template can never format a value some other way and disagree with the
    engine about what a number is.
    """
    from massingbill.services.money import Bp, Cents, format_bp, to_display
    from massingbill.services.rbac import active_organization, has_permission

    @app.template_filter("money")
    def _money(value: int | None) -> str:
        if value is None:
            return "—"
        return to_display(Cents(int(value)))

    @app.template_filter("money_paren")
    def _money_paren(value: int | None) -> str:
        if value is None:
            return "—"
        return to_display(Cents(int(value)), parens_for_negative=True)

    @app.template_filter("pct")
    def _pct(value: int | None) -> str:
        if value is None:
            return "—"
        return format_bp(Bp(int(value)))

    app.jinja_env.globals["has_permission"] = has_permission
    app.jinja_env.globals["active_organization"] = active_organization
