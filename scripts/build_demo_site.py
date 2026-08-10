#!/usr/bin/env python
"""Build the static demo site published to GitHub Pages.

Runs the demo project through the real engine and writes its *actual* rendered
output. Nothing here is mocked up: every figure on the published pages came out
of the same code a customer would run, which is the only kind of demo that is
worth anything for a product whose claim is that the numbers tie.

    python scripts/build_demo_site.py [output-dir]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from massingbill import create_app
from massingbill.config import Settings
from massingbill.extensions import db
from massingbill.models import Application
from massingbill.services import application as app_service
from massingbill.services import demo, tieout
from massingbill.services import waivers as waiver_service
from massingbill.services.money import Cents, to_display
from massingbill.services.renderers import PDF_AVAILABLE, available_formats
from massingbill.services.renderers.documents import Format, render

HERE = Path(__file__).resolve().parent


@dataclass
class Page:
    """One application, as the index page needs it."""

    number: int
    period: str
    status: str
    completed: str
    certified: str
    tieout: str
    tieout_ok: bool
    files: dict[str, str] = field(default_factory=dict)

    @property
    def downloads(self) -> list[tuple[str, str]]:
        """The non-HTML renderings, which are links rather than the row target."""
        return [(fmt, path) for fmt, path in sorted(self.files.items()) if fmt != "html"]


def build(output: Path) -> None:
    workspace = Path(tempfile.mkdtemp(prefix="massingbill-demo-"))
    settings = Settings(
        env="testing",
        secret_key="demo-site-build-only",
        instance_path=workspace,
        database_url="sqlite:///:memory:",
        log_json=False,
        log_level="ERROR",
    )
    app = create_app(settings)

    with app.app_context():
        db.create_all()
        built = demo.build()

        output.mkdir(parents=True, exist_ok=True)
        (output / "documents").mkdir(exist_ok=True)

        pages = [
            _render_application(application, output)
            for application in app_service.applications_for(built.contract)
        ]

        unverified = waiver_service.unverified_templates(built.organization.id)
        page = (
            _environment()
            .get_template("demo_site.html.j2")
            .render(
                pages=pages,
                project_number=built.project.number,
                project_name=built.project.name,
                contract_sum=to_display(Cents(demo.CONTRACT_SUM)),
                built_on=date.today().isoformat(),
                unverified=len(unverified),
                waiver_refusal=built.waiver_refusal,
                pdf_available=PDF_AVAILABLE,
            )
        )
        (output / "index.html").write_text(page, encoding="utf-8")
        (output / ".nojekyll").write_text("", encoding="utf-8")

    shutil.rmtree(workspace, ignore_errors=True)
    print(f"Demo site written to {output.resolve()} ({len(pages)} applications).")


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(HERE),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_application(application: Application, output: Path) -> Page:
    """Write every available rendering of one application and describe it."""
    report = tieout.run(application)
    page = Page(
        number=application.number,
        period=(f"{application.period_start.isoformat()} to {application.period_end.isoformat()}"),
        status=str(application.status),
        completed=to_display(Cents(application.line4_completed_stored)),
        certified=to_display(Cents(application.certified_payment_cents)),
        tieout=report.summary(),
        tieout_ok=report.ok,
    )

    for fmt in available_formats():
        document = render(application, fmt)
        name = (
            f"documents/application-{page.number:03d}.html"
            if fmt == Format.HTML
            else f"documents/{document.filename}"
        )
        (output / name).write_bytes(document.content)
        page.files["html" if fmt == Format.HTML else str(fmt)] = name

    return page


if __name__ == "__main__":
    # An unset shell variable arrives as "", and Path("") is the *current*
    # directory -- which would strew a hundred rendered documents through the
    # working tree. Refuse it rather than accept it as a default.
    if len(sys.argv) > 1 and not sys.argv[1].strip():
        sys.exit("Output directory is empty. Pass a path, or omit it for ./site.")

    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("site"))
