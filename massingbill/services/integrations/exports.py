"""File-based export to accounting and portal systems.

An **optional integration**. The core never imports this package; CI empties it
and re-runs the suite (SPEC.md 3, 13).

File-based first, on purpose. Sage 300 CRE, Viewpoint Vista and CMiC all have
APIs, and construction accounting departments overwhelmingly do not use them --
they import a file, because that is what their controller can check before it
touches the ledger. An API adapter that posts straight into the general ledger
is a harder sell than a CSV somebody can open.

Every export is **derived, never re-computed**. The figures come from the frozen
application exactly as they were certified; nothing here adds, allocates or
rounds. An export that disagrees with the document it was exported from is worse
than no export, because the disagreement surfaces inside somebody else's system
where nobody can see where it came from.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from massingbill.models import Application
from massingbill.services.money import Cents, to_decimal


class ExportTarget(StrEnum):
    """The systems we can hand a file to."""

    SAGE_300_CRE = "sage_300_cre"
    VIEWPOINT_VISTA = "viewpoint_vista"
    QUICKBOOKS = "quickbooks"
    #: Owner-mandated portals. A GC using Massing Bill still has owners who
    #: require Textura; exporting into their upload format means adopting this
    #: never requires abandoning that.
    TEXTURA = "textura"
    GCPAY = "gcpay"

    @property
    def label(self) -> str:
        return EXPORT_LABELS[self]


EXPORT_LABELS = {
    ExportTarget.SAGE_300_CRE: "Sage 300 CRE",
    ExportTarget.VIEWPOINT_VISTA: "Viewpoint Vista",
    ExportTarget.QUICKBOOKS: "QuickBooks",
    ExportTarget.TEXTURA: "Textura",
    ExportTarget.GCPAY: "GCPay",
}


@dataclass(frozen=True)
class ExportFile:
    filename: str
    content: bytes
    content_type: str = "text/csv"


def _amount(value: int) -> str:
    """A decimal string, never a float.

    Receiving systems parse text. Handing them a float's repr is how ``1234.57``
    arrives as ``1234.5700000000001`` and a controller spends an afternoon on it.
    """
    return str(to_decimal(Cents(int(value))))


def _rows_to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    # CRLF and a BOM: both of these systems are Windows-first, and Excel opens a
    # UTF-8 file without a BOM as Latin-1, which turns every em dash in a line
    # description into mojibake on the controller's screen.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


# ── The exporters ───────────────────────────────────────────────────────────


def _job_cost_rows(application: Application) -> list[list[str]]:
    """One row per schedule line, the shape every job-cost import expects."""
    return [
        [
            application.prime_contract.project.number,
            line.item_no,
            line.description,
            line.csi_code or "",
            _amount(line.col_c_scheduled_value),
            _amount(line.col_e_this_period),
            _amount(line.col_f_stored),
            _amount(line.col_g_completed_stored),
            _amount(line.col_i_retainage),
        ]
        for line in application.lines
    ]


def _sage(application: Application) -> ExportFile:
    header = [
        "Job",
        "CostCode",
        "Description",
        "Category",
        "ContractAmount",
        "AmountThisPeriod",
        "StoredMaterials",
        "CompletedToDate",
        "RetainageHeld",
    ]
    return ExportFile(
        filename=f"sage-{application.prime_contract.project.number}-{application.number:03d}.csv",
        content=_rows_to_csv(header, _job_cost_rows(application)),
    )


def _vista(application: Application) -> ExportFile:
    header = [
        "JCCo",
        "Contract",
        "Item",
        "Description",
        "CostType",
        "ContractAmt",
        "CurrentAmt",
        "StoredMatl",
        "TotalCompleted",
        "RetainageAmt",
    ]
    rows = [
        ["1", application.prime_contract.project.number, *row[1:]]
        for row in _job_cost_rows(application)
    ]
    return ExportFile(
        filename=f"vista-{application.prime_contract.project.number}-{application.number:03d}.csv",
        content=_rows_to_csv(header, rows),
    )


def _quickbooks(application: Application) -> ExportFile:
    """A single invoice line per schedule line, plus a retainage line.

    QuickBooks has no retainage concept, so the convention every construction
    bookkeeper uses is a negative line against a retainage-receivable item. That
    is what makes the invoice total equal the amount actually due, which is the
    number the customer will pay against.
    """
    project = application.prime_contract.project
    header = ["Customer", "InvoiceNo", "Date", "Item", "Description", "Amount"]

    rows: list[list[str]] = [
        [
            project.name,
            f"{project.number}-{application.number:03d}",
            application.period_end.isoformat(),
            line.item_no,
            line.description,
            _amount(line.col_e_this_period + line.col_f_stored),
        ]
        for line in application.lines
        if line.col_e_this_period or line.col_f_stored
    ]

    if application.line5_total_retainage:
        rows.append(
            [
                project.name,
                f"{project.number}-{application.number:03d}",
                application.period_end.isoformat(),
                "RETAINAGE",
                "Retainage withheld this application",
                _amount(-application.line5_total_retainage),
            ]
        )

    return ExportFile(
        filename=f"quickbooks-{project.number}-{application.number:03d}.csv",
        content=_rows_to_csv(header, rows),
    )


def _portal(application: Application, target: ExportTarget) -> ExportFile:
    """Textura and GCPay upload shape.

    Their formats are undocumented and change without notice, so this is
    deliberately a plain G703-ordered sheet rather than an attempt at their
    exact spec. It is never on the critical path: a period can always be closed
    without it, and a controller can always retype from the PDF.
    """
    header = [
        "ItemNo",
        "Description",
        "ScheduledValue",
        "PreviousApplications",
        "ThisPeriod",
        "MaterialsPresentlyStored",
        "TotalCompletedAndStored",
        "PercentComplete",
        "BalanceToFinish",
        "Retainage",
    ]
    rows = [
        [
            line.item_no,
            line.description,
            _amount(line.col_c_scheduled_value),
            _amount(line.col_d_previous),
            _amount(line.col_e_this_period),
            _amount(line.col_f_stored),
            _amount(line.col_g_completed_stored),
            f"{line.percent_complete_bp / 100:.2f}",
            _amount(line.col_h_balance),
            _amount(line.col_i_retainage),
        ]
        for line in application.lines
    ]
    project = application.prime_contract.project
    return ExportFile(
        filename=f"{target}-{project.number}-{application.number:03d}.csv",
        content=_rows_to_csv(header, rows),
    )


_EXPORTERS = {
    ExportTarget.SAGE_300_CRE: _sage,
    ExportTarget.VIEWPOINT_VISTA: _vista,
    ExportTarget.QUICKBOOKS: _quickbooks,
}


def export(application: Application, target: ExportTarget) -> ExportFile:
    """Render one application for one receiving system."""
    chosen = ExportTarget(target)
    if chosen in _EXPORTERS:
        return _EXPORTERS[chosen](application)
    return _portal(application, chosen)


__all__ = [
    "EXPORT_LABELS",
    "ExportFile",
    "ExportTarget",
    "export",
]
