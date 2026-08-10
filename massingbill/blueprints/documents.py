"""Downloading a rendered application."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, abort
from flask_login import login_required

from massingbill.errors import NotFoundError
from massingbill.models import Application, Project
from massingbill.services import application as app_service
from massingbill.services.rbac import SOV_READ, get_scoped_or_404, require_permission
from massingbill.services.renderers.documents import Format, available_formats, render

bp = Blueprint("documents", __name__, url_prefix="/projects/<project_id>/applications")


def _application(project_id: str, application_id: str) -> Application:
    project = get_scoped_or_404(Project, project_id)
    application = get_scoped_or_404(Application, application_id)
    if application.prime_contract_id != (
        project.prime_contract.id if project.prime_contract else None
    ):
        # The application exists in this tenant but belongs to another project.
        raise NotFoundError("No such application on this project.")
    return application


@bp.get("/<application_id>/download.<fmt>")
@login_required
@require_permission(SOV_READ)
def download(project_id: str, application_id: str, fmt: str) -> Any:
    """Render and return one format.

    Rendered on demand rather than served from storage: a draft changes between
    requests, and a submitted application renders from its frozen snapshot, so
    the result is stable for exactly the documents that need to be.
    """
    application = _application(project_id, application_id)

    try:
        wanted = Format(fmt)
    except ValueError:
        abort(404)

    if wanted not in available_formats():
        # Naming what is missing beats a generic 500 from a native library.
        from massingbill.services.renderers import (
            PDF_AVAILABLE,
            pdf_unavailable_reason,
            xlsx_unavailable_reason,
        )

        reason = pdf_unavailable_reason() if not PDF_AVAILABLE else xlsx_unavailable_reason()
        abort(503, description=reason or f"{wanted} rendering is not available here.")

    document = render(application, wanted)

    disposition = "inline" if wanted in (Format.HTML, Format.PDF) else "attachment"
    return Response(
        document.content,
        mimetype=document.content_type.split(";")[0],
        headers={
            "Content-Type": document.content_type,
            "Content-Disposition": f'{disposition}; filename="{document.filename}"',
            "Content-Length": str(document.size),
            # Lets a caller prove the bytes they hold are the bytes we produced.
            "X-Document-SHA256": document.sha256,
        },
    )


__all__ = ["app_service", "bp"]
