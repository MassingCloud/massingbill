"""Assembling and storing a document package.

One entry point, :func:`render`, so a caller never has to know which module
produces which format, and every format is built from the same view (see
``context.py``) rather than assembling its own numbers.

Rendered documents are written through the storage backend and recorded, so a
package that was issued to an owner can be produced again byte-for-byte and
shown to have been the one that was sent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

from flask import current_app

from massingbill.errors import ValidationError
from massingbill.models import Application
from massingbill.services.renderers import PDF_AVAILABLE, XLSX_AVAILABLE
from massingbill.services.renderers import context as context_module
from massingbill.services.storage import StoragePointer


class Format(StrEnum):
    PDF = "pdf"
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"
    HTML = "html"


CONTENT_TYPES = {
    Format.PDF: "application/pdf",
    Format.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    Format.CSV: "text/csv; charset=utf-8",
    Format.JSON: "application/json",
    Format.HTML: "text/html; charset=utf-8",
}


@dataclass(frozen=True)
class RenderedDocument:
    """A produced document, plus the digest that identifies it."""

    fmt: Format
    filename: str
    content: bytes
    content_type: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class DocumentPackage:
    application_id: str
    documents: list[RenderedDocument]

    def by_format(self, fmt: Format) -> RenderedDocument | None:
        return next((d for d in self.documents if d.fmt == fmt), None)


def available_formats() -> list[Format]:
    """What this deployment can actually produce.

    A deployment missing the native PDF stack should say so rather than
    offering a button that fails.
    """
    formats = [Format.CSV, Format.JSON, Format.HTML]
    if XLSX_AVAILABLE:
        formats.insert(0, Format.XLSX)
    if PDF_AVAILABLE:
        formats.insert(0, Format.PDF)
    return formats


def filename_for(application: Application, fmt: Format) -> str:
    project = application.prime_contract.project if application.prime_contract else None
    number = f"{application.number:03d}"
    stem = f"{project.number}-pay-app-{number}" if project else f"pay-app-{number}"
    return f"{stem}.{fmt}"


def render(application: Application, fmt: Format | str) -> RenderedDocument:
    """Produce one document."""
    fmt = Format(fmt)
    view = context_module.build(application)

    if fmt == Format.PDF:
        from massingbill.services.renderers import pdf

        content = pdf.render(view)
    elif fmt == Format.HTML:
        from massingbill.services.renderers import pdf

        content = pdf.render_html(view).encode("utf-8")
    elif fmt == Format.XLSX:
        if not XLSX_AVAILABLE:
            from massingbill.services.renderers import xlsx_unavailable_reason

            raise ValidationError(xlsx_unavailable_reason())
        from massingbill.services.renderers import xlsx

        content = xlsx.render(view)
    elif fmt == Format.CSV:
        from massingbill.services.renderers import tabular

        content = tabular.render_csv(view)
    else:
        from massingbill.services.renderers import tabular

        content = tabular.render_json(view)

    return RenderedDocument(
        fmt=fmt,
        filename=filename_for(application, fmt),
        content=content,
        content_type=CONTENT_TYPES[fmt],
    )


def render_package(
    application: Application, formats: list[Format] | None = None
) -> DocumentPackage:
    """Produce every format this deployment supports."""
    wanted = formats if formats is not None else available_formats()
    return DocumentPackage(
        application_id=application.id,
        documents=[render(application, fmt) for fmt in wanted],
    )


def store(application: Application, document: RenderedDocument) -> StoragePointer:
    """Write a rendered document through the configured storage backend.

    Keyed by organization and application so a tenant's documents are
    contiguous on disk and an export is a directory copy.
    """
    backend = current_app.extensions["massingbill_storage"]
    key = f"{application.organization_id}/applications/{application.id}/{document.filename}"
    pointer: StoragePointer = backend.put(
        key, BytesIO(document.content), content_type=document.content_type
    )
    return pointer
