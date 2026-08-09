"""Document renderers.

Availability is detected at import and reported honestly. WeasyPrint needs a
native stack (pango, cairo, harfbuzz) that a Windows workstation or a slim
container will not have, and the failure mode that matters is the quiet one:
a deployment where PDF export silently stops working and nobody notices until
an owner asks where the pay application is.

So the check is explicit, the reason is preserved, and CI asserts that the
PDF-marked tests actually *ran* rather than skipping.
"""

from __future__ import annotations

import contextlib
import io

PDF_AVAILABLE = True
_PDF_REASON = ""

try:  # pragma: no cover - the branch taken depends on the host
    # WeasyPrint prints a multi-line installation notice straight to stderr
    # when its native libraries are missing. We report the reason ourselves,
    # through `pdf_unavailable_reason`, so swallow the banner rather than
    # having every CLI invocation on a workstation start with it.
    with contextlib.redirect_stderr(io.StringIO()):
        import weasyprint  # noqa: F401
except Exception as exc:  # noqa: BLE001 - any import failure means unavailable
    PDF_AVAILABLE = False
    _PDF_REASON = (
        f"WeasyPrint could not be loaded ({type(exc).__name__}: {exc}). "
        "PDF rendering needs the native pango/cairo stack: install "
        "massingbill[render] plus the system libraries, or use the container "
        "image, which carries them."
    )


def pdf_unavailable_reason() -> str:
    """Why PDF rendering is off, for a log line or an admin screen."""
    return _PDF_REASON


XLSX_AVAILABLE = True
_XLSX_REASON = ""

try:  # pragma: no cover
    import openpyxl  # noqa: F401
except Exception as exc:  # noqa: BLE001
    XLSX_AVAILABLE = False
    _XLSX_REASON = (
        f"openpyxl could not be loaded ({type(exc).__name__}: {exc}). Install massingbill[render]."
    )


def xlsx_unavailable_reason() -> str:
    return _XLSX_REASON


from .documents import (  # noqa: E402
    DocumentPackage,
    RenderedDocument,
    available_formats,
    render,
)

__all__ = [
    "PDF_AVAILABLE",
    "XLSX_AVAILABLE",
    "DocumentPackage",
    "RenderedDocument",
    "available_formats",
    "pdf_unavailable_reason",
    "render",
    "xlsx_unavailable_reason",
]
