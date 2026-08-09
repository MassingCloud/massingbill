"""The PDF renderer.

WeasyPrint over a Jinja template, so the document is HTML and CSS and can be
reviewed in a browser before anyone waits on a native stack.

**On reproducibility.** The rendered *content* is identical on every run --
nothing in the template varies. The raw bytes are not, because WeasyPrint
stamps a PDF creation timestamp; set ``SOURCE_DATE_EPOCH`` and they are, which
is what an archival workflow wants.

Byte-equality is not, however, what shows a document to be the one that was
issued. The application's snapshot fingerprint does that, and it covers the
numbers rather than the rendering -- so it survives a WeasyPrint upgrade that
shifts a kerning pair, which byte-equality would not.
"""

from __future__ import annotations

from flask import render_template

from massingbill.errors import AdapterUnavailableError
from massingbill.models import FormStyle
from massingbill.services.renderers import PDF_AVAILABLE, pdf_unavailable_reason
from massingbill.services.renderers.context import ApplicationView

#: Templates per style. `custom` falls back to the house form until a customer
#: template is uploaded (P6) -- rendering nothing would be worse than rendering
#: something correct in the wrong livery.
_TEMPLATES = {
    FormStyle.AIA_STYLE: "documents/aia_style.html",
    FormStyle.HOUSE: "documents/house.html",
    FormStyle.CUSTOM: "documents/house.html",
}


def template_for(style: str) -> str:
    try:
        return _TEMPLATES[FormStyle(style)]
    except (ValueError, KeyError):
        return _TEMPLATES[FormStyle.AIA_STYLE]


def render_html(view: ApplicationView) -> str:
    """The document as HTML. Useful on its own: it previews in a browser."""
    return render_template(template_for(view.form_style), view=view)


def render(view: ApplicationView) -> bytes:
    """The document as PDF bytes."""
    if not PDF_AVAILABLE:
        raise AdapterUnavailableError(pdf_unavailable_reason())

    from weasyprint import HTML

    rendered: bytes = HTML(string=render_html(view)).write_pdf()
    return rendered
