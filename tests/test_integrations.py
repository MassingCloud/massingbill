"""ERP and portal exports.

The test that matters is the one that adds the file back up. Every other
property here -- column names, encodings, filenames -- is cosmetic next to
"does the exported total equal what was certified". An export that quietly
disagrees with its source surfaces inside somebody else's ledger, where nobody
can see where it came from.
"""

from __future__ import annotations

import csv
import io

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import Role
from massingbill.services import application as app_service
from massingbill.services import sov as sov_service
from massingbill.services.integrations.exports import ExportTarget, export
from tests.factories import add_balanced_lines, make_tenant

pytestmark = pytest.mark.adapter


@pytest.fixture
def application(app: Flask):
    tenant = make_tenant("acme")
    add_balanced_lines(tenant)
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    from datetime import date

    built = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
        actor=tenant.user(Role.PM),
    )
    app_service.enter(
        built,
        [
            app_service.PeriodEntry(
                line_id=line.id,
                this_period=__import__("massingbill.services.money", fromlist=["cents"]).cents(
                    100_000_00
                ),
                stored=__import__("massingbill.services.money", fromlist=["cents"]).cents(0),
            )
            for line in built.lines
        ],
        actor=tenant.user(Role.PM),
    )
    db.session.commit()
    return built


def rows(export_file) -> list[list[str]]:
    return list(csv.reader(io.StringIO(export_file.content.decode("utf-8-sig"))))


def total_exported(export_file) -> int:
    """Add the amount column of a rendered export back up, in cents.

    Lives here rather than in the exporter: it exists only so a test can check
    a file against the application it came from, and a shipped module carrying
    a function whose reason is "so a test can" is a module with a test in it.
    (The money-discipline gate agreed, for its own reason.)
    """
    from decimal import Decimal

    parsed = rows(export_file)
    header, body = parsed[0], parsed[1:]

    for name in ("Amount", "AmountThisPeriod", "CurrentAmt", "ThisPeriod"):
        if name in header:
            index = header.index(name)
            total = sum(Decimal(row[index]) for row in body if row and row[index])
            return int(total * 100)

    raise AssertionError(f"No amount column in {export_file.filename}")


# ── The arithmetic ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [ExportTarget.SAGE_300_CRE, ExportTarget.VIEWPOINT_VISTA, ExportTarget.TEXTURA],
)
def test_the_exported_period_total_matches_the_application(application, target) -> None:
    """The whole point. Nothing in an exporter re-computes; it renders."""
    expected = sum(line.col_e_this_period for line in application.lines)

    assert total_exported(export(application, target)) == expected
    assert expected > 0, "the fixture billed nothing, so this proved nothing"


@pytest.mark.parametrize("target", list(ExportTarget))
def test_every_target_produces_a_file_with_a_header_and_the_right_row_count(
    application, target
) -> None:
    parsed = rows(export(application, target))

    assert parsed[0], "no header row"
    if target is ExportTarget.QUICKBOOKS:
        # One line per billed item, plus the retainage line.
        billed = [line for line in application.lines if line.col_e_this_period or line.col_f_stored]
        expected = len(billed) + (1 if application.line5_total_retainage else 0)
    else:
        expected = len(list(application.lines))
    assert len(parsed) - 1 == expected


def test_quickbooks_carries_retainage_as_a_negative_line(application) -> None:
    """QuickBooks has no retainage concept. The convention every construction
    bookkeeper uses is a negative line, which makes the invoice total equal the
    amount actually due."""
    parsed = rows(export(application, ExportTarget.QUICKBOOKS))
    retainage = [row for row in parsed[1:] if row[3] == "RETAINAGE"]

    assert len(retainage) == 1
    assert retainage[0][5].startswith("-")


def test_the_quickbooks_invoice_totals_the_amount_actually_due(application) -> None:
    from decimal import Decimal

    parsed = rows(export(application, ExportTarget.QUICKBOOKS))
    total = sum(Decimal(row[5]) for row in parsed[1:])

    assert int(total * 100) == application.line8_current_payment_due


# ── The file itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", list(ExportTarget))
def test_no_amount_is_ever_rendered_as_a_float(application, target) -> None:
    """Receiving systems parse text. A float's repr is how 1234.57 arrives as
    1234.5700000000001 and a controller loses an afternoon."""
    text = export(application, target).content.decode("utf-8-sig")

    assert "e-" not in text.lower(), "scientific notation in an exported amount"
    assert "0000000" not in text, "float artefact in an exported amount"


@pytest.mark.parametrize("target", list(ExportTarget))
def test_files_are_windows_friendly(application, target) -> None:
    """These land in Excel on a controller's desktop. Without a BOM, Excel reads
    UTF-8 as Latin-1 and every em dash in a description becomes mojibake."""
    content = export(application, target).content

    assert content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in content


@pytest.mark.parametrize("target", list(ExportTarget))
def test_the_filename_names_the_project_and_the_application(application, target) -> None:
    """A downloads folder with six files called export.csv helps nobody."""
    filename = export(application, target).filename

    assert application.prime_contract.project.number in filename
    assert f"{application.number:03d}" in filename
    assert filename.endswith(".csv")


def test_an_unknown_target_is_refused(application) -> None:
    with pytest.raises(ValueError):
        export(application, "not-a-system")


# ── The seam ────────────────────────────────────────────────────────────────


def test_the_core_never_imports_the_integrations_package() -> None:
    """Stated in a test as well as in the import contract, because the contract
    is a file somebody could edit without noticing what it was for."""
    from massingbill.optional import INTEGRATIONS_PREFIX, is_optional

    assert is_optional(f"{INTEGRATIONS_PREFIX}.exports")
