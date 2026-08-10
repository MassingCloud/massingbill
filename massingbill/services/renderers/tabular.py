"""CSV and JSON output.

The CSV column order matches the G703 exactly -- A, B, C, D, E, F, G, %, H, I.
That is deliberate and documented (docs/legal-forms-policy.md): it is the
**safe path**, the export a customer who holds an AIA licence uses to populate
their own official document without needing anything we render.

Amounts are written as decimal strings rather than floats. A spreadsheet that
opens the file will parse them correctly, and nothing in the pipeline ever turns
a cent into a binary fraction.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any

from massingbill.services.money import cents, to_decimal
from massingbill.services.renderers.context import ApplicationView, as_json

#: G703 column order. Changing this breaks the safe path, so it is named rather
#: than inlined.
G703_COLUMNS = [
    "A Item No.",
    "B Description of Work",
    "CSI",
    "C Scheduled Value",
    "D From Previous Application",
    "E This Period",
    "F Materials Presently Stored",
    "G Total Completed and Stored to Date",
    "% (G/C)",
    "H Balance to Finish",
    "I Retainage",
]


def _amount(value: int) -> str:
    return str(to_decimal(cents(value)))


def render_csv(view: ApplicationView) -> bytes:
    """The continuation sheet as CSV, in G703 column order."""
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    writer.writerow([f"Application for Payment No. {view.number}"])
    writer.writerow([f"{view.project_number} — {view.project_name}"])
    writer.writerow([f"Period: {view.period_label}"])
    writer.writerow([])
    writer.writerow(G703_COLUMNS)

    for line in view.lines:
        writer.writerow(
            [
                line.item_no,
                line.description,
                line.csi_code,
                _amount(line.c),
                _amount(line.d),
                _amount(line.e),
                _amount(line.f),
                _amount(line.g),
                f"{line.percent_bp / 100:.2f}%",
                _amount(line.h),
                _amount(line.i),
            ]
        )

    writer.writerow(
        [
            "",
            "TOTALS",
            "",
            _amount(view.line_total("c")),
            _amount(view.line_total("d")),
            _amount(view.line_total("e")),
            _amount(view.line_total("f")),
            _amount(view.line_total("g")),
            "",
            _amount(view.line_total("h")),
            _amount(view.line_total("i")),
        ]
    )

    writer.writerow([])
    writer.writerow([view.disclaimer])

    # BOM so Excel opens UTF-8 correctly on Windows without an import dialog.
    return buffer.getvalue().encode("utf-8-sig")


def render_json(view: ApplicationView) -> bytes:
    payload: dict[str, Any] = as_json(view)
    return json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")
