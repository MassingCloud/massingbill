"""The Flask application factory.

Split out of ``massingbill/__init__.py`` so that importing
``massingbill.core`` -- which must load with nothing but the standard library --
does not run Flask at import time. The package ``__init__`` resolves
``create_app`` lazily; see the note there.
"""

from __future__ import annotations

from typing import Any

from flask import Flask

from massingbill import __version__


def create_app(settings: Any = None, **overrides: Any) -> Flask:
    """Build a configured application.

    Args:
        settings: A prepared ``Settings`` instance. Tests pass one directly so
            they never depend on the ambient environment.
        **overrides: Field overrides applied when ``settings`` is not given.
    """
    from massingbill.blueprints import register_blueprints, register_template_helpers
    from massingbill.config import Settings, load_settings
    from massingbill.errors import register_error_handlers
    from massingbill.extensions import csrf, db, limiter, login_manager, migrate
    from massingbill.logging_config import configure_logging
    from massingbill.security import register_security_headers
    from massingbill.services.entitlement import get_provider as get_entitlement_provider
    from massingbill.services.storage import get_backend as get_storage_backend

    config: Settings = settings if settings is not None else load_settings(**overrides)

    app = Flask(__name__, instance_relative_config=False)
    app.config.update(config.flask_config())
    app.config["MASSINGBILL_VERSION"] = __version__
    app.config["MASSINGBILL_ENTITLEMENT_PROVIDER"] = config.entitlement_provider
    app.config["MASSINGBILL_STORAGE_BACKEND"] = config.storage_backend
    app.config["MASSINGBILL_SETTINGS"] = config

    configure_logging(app, level=config.log_level, as_json=config.log_json)

    config.instance_path.mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.sign_in_form"
    login_manager.login_message = "Sign in to continue."
    login_manager.session_protection = "strong"
    limiter.init_app(app)

    # Import for metadata registration; Alembic autogenerate needs every mapped
    # class present before it compares against the database.
    import massingbill.models  # noqa: F401

    # Resolve the adapters once, at boot. In a standalone install this touches
    # no network and imports no optional dependency.
    app.extensions["massingbill_entitlement"] = get_entitlement_provider(
        config.entitlement_provider
    )
    app.extensions["massingbill_storage"] = get_storage_backend(
        config.storage_backend,
        **({"root": config.instance_path / "storage"} if config.storage_backend == "local" else {}),
    )

    register_security_headers(app)
    register_error_handlers(app)
    register_blueprints(app)
    register_template_helpers(app)

    from massingbill.blueprints.auth import register_login_manager
    from massingbill.services.rbac import register_request_hooks

    register_login_manager(login_manager)
    register_request_hooks(app)

    app.logger.info(
        "massingbill %s ready (entitlement=%s storage=%s env=%s)",
        __version__,
        config.entitlement_provider,
        config.storage_backend,
        config.env,
    )
    return app
