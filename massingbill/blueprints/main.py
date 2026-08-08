"""The public shell.

P0 ships one page so the skeleton is demonstrably alive end to end -- factory,
templating, static assets, security headers and error pages. Projects, SOVs and
applications replace this in P2 onward.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

bp = Blueprint("main", __name__)


@bp.get("/")
def index() -> str:
    return render_template(
        "index.html",
        version=current_app.config["MASSINGBILL_VERSION"],
        entitlement_provider=current_app.config["MASSINGBILL_ENTITLEMENT_PROVIDER"],
        storage_backend=current_app.config["MASSINGBILL_STORAGE_BACKEND"],
    )
