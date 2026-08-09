"""The view model every renderer reads from.

One structure, four outputs. A PDF, a spreadsheet, a CSV and a JSON payload all
describe the same application, and if each built its own numbers they would
eventually disagree -- which is precisely the failure this product exists to
prevent. So the numbers are assembled once, here.

**A submitted application is rendered from its snapshot, not from live rows.**
That is the whole point of taking one: the schedule of values gets revised, the
retainage rule gets switched to stepped, later periods get certified, and the
issued document must still say what it said.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from massingbill.models import Application, FormStyle
from massingbill.services import tieout
from massingbill.services.money import Bp, Cents, cents, format_bp, to_decimal, to_display

#: Carried on every rendered document produced by the AIA-style renderer, and
#: not configurable. See docs/legal-forms-policy.md -- we model the line
#: structure and the arithmetic, which is not what copyright protects, and we
#: say so on the face of the document.
AIA_DISCLAIMER = (
    "Prepared with Massing Bill. Format follows the AIA G702/G703 line "
    "structure. Massing Bill is not affiliated with, endorsed by, or sponsored "
    "by The American Institute of Architects. AIA®, G702® and "
    "G703® are registered trademarks of the AIA."
)


@dataclass(frozen=True)
class LineView:
    """One G703 row, formatted and raw."""

    item_no: str
    description: str
    csi_code: str
    c: Cents
    d: Cents
    e: Cents
    f: Cents
    g: Cents
    h: Cents
    i: Cents
    percent_bp: Bp

    def money(self, column: str) -> str:
        value = getattr(self, column)
        return to_display(cents(value)) if value else ""

    @property
    def percent(self) -> str:
        return format_bp(self.percent_bp)


@dataclass(frozen=True)
class HeaderView:
    """The nine G702 lines, plus the change-order summary box."""

    line1: Cents
    line2: Cents
    line3: Cents
    line4: Cents
    line5a: Cents
    line5b: Cents
    line5: Cents
    line6: Cents
    line7: Cents
    line8: Cents
    line9: Cents
    co_prev_additions: Cents
    co_prev_deductions: Cents
    co_this_additions: Cents
    co_this_deductions: Cents

    def money(self, line: str) -> str:
        return to_display(cents(getattr(self, line)))

    @property
    def co_net(self) -> Cents:
        return cents(
            self.co_prev_additions
            - self.co_prev_deductions
            + self.co_this_additions
            - self.co_this_deductions
        )


@dataclass
class ApplicationView:
    """Everything a renderer needs, assembled once."""

    number: int
    period_start: date
    period_end: date
    application_date: date
    status: str
    form_style: str
    from_snapshot: bool

    project_number: str
    project_name: str
    project_address: str
    contract_number: str

    retainage_work: Bp
    retainage_stored: Bp

    header: HeaderView
    lines: list[LineView]
    findings: list[dict[str, Any]] = field(default_factory=list)
    tieout_summary: str = ""
    tieout_ok: bool = True

    disclaimer: str = AIA_DISCLAIMER

    @property
    def title(self) -> str:
        return f"Application for Payment No. {self.number}"

    @property
    def period_label(self) -> str:
        return f"{self.period_start.isoformat()} to {self.period_end.isoformat()}"

    def line_total(self, column: str) -> Cents:
        return cents(sum(getattr(line, column) for line in self.lines))


def build(application: Application, *, include_tieout: bool = True) -> ApplicationView:
    """Assemble the view for one application."""
    contract = application.prime_contract
    project = contract.project if contract is not None else None
    rule = contract.retainage_rule if contract is not None else None

    snapshot = application.snapshot
    use_snapshot = snapshot is not None and not application.is_editable

    if use_snapshot and snapshot is not None:
        header, lines, rates = _from_snapshot(json.loads(snapshot.payload))
    else:
        header, lines, rates = _from_live(application, rule)

    view = ApplicationView(
        number=application.number,
        period_start=application.period_start,
        period_end=application.period_end,
        application_date=application.application_date,
        status=str(application.status),
        form_style=str(application.form_style),
        from_snapshot=use_snapshot,
        project_number=project.number if project else "",
        project_name=project.name if project else "",
        project_address=project.address if project else "",
        contract_number=contract.number if contract else "",
        retainage_work=rates[0],
        retainage_stored=rates[1],
        header=header,
        lines=lines,
    )

    if include_tieout:
        report = tieout.run(application)
        view.findings = [f.as_dict() for f in report.findings]
        view.tieout_summary = report.summary()
        view.tieout_ok = report.ok

    return view


def _from_live(
    application: Application, rule: Any
) -> tuple[HeaderView, list[LineView], tuple[Bp, Bp]]:
    header = HeaderView(
        line1=cents(application.line1_original_sum),
        line2=cents(application.line2_net_co),
        line3=cents(application.line3_contract_sum_to_date),
        line4=cents(application.line4_completed_stored),
        line5a=cents(application.line5a_retainage_work),
        line5b=cents(application.line5b_retainage_stored),
        line5=cents(application.line5_total_retainage),
        line6=cents(application.line6_earned_less_retainage),
        line7=cents(application.line7_previous_certificates),
        line8=cents(application.line8_current_payment_due),
        line9=cents(application.line9_balance_to_finish),
        co_prev_additions=cents(application.co_summary_prev_additions),
        co_prev_deductions=cents(application.co_summary_prev_deductions),
        co_this_additions=cents(application.co_summary_this_additions),
        co_this_deductions=cents(application.co_summary_this_deductions),
    )
    lines = [
        LineView(
            item_no=line.item_no,
            description=line.description,
            csi_code=line.csi_code,
            c=cents(line.col_c_scheduled_value),
            d=cents(line.col_d_previous),
            e=cents(line.col_e_this_period),
            f=cents(line.col_f_stored),
            g=cents(line.col_g_completed_stored),
            h=cents(line.col_h_balance),
            i=cents(line.col_i_retainage),
            percent_bp=Bp(line.percent_complete_bp),
        )
        for line in application.lines
    ]
    rates = (
        Bp(rule.rate_work_bp) if rule is not None else Bp(0),
        Bp(rule.rate_stored_bp) if rule is not None else Bp(0),
    )
    return header, lines, rates


def _from_snapshot(payload: dict[str, Any]) -> tuple[HeaderView, list[LineView], tuple[Bp, Bp]]:
    app = payload["application"]
    summary = app["co_summary"]
    rule = payload.get("retainage_rule") or {}

    header = HeaderView(
        line1=cents(app["line1_original_sum"]),
        line2=cents(app["line2_net_co"]),
        line3=cents(app["line3_contract_sum_to_date"]),
        line4=cents(app["line4_completed_stored"]),
        line5a=cents(app["line5a_retainage_work"]),
        line5b=cents(app["line5b_retainage_stored"]),
        line5=cents(app["line5_total_retainage"]),
        line6=cents(app["line6_earned_less_retainage"]),
        line7=cents(app["line7_previous_certificates"]),
        line8=cents(app["line8_current_payment_due"]),
        line9=cents(app["line9_balance_to_finish"]),
        co_prev_additions=cents(summary["prev_additions"]),
        co_prev_deductions=cents(summary["prev_deductions"]),
        co_this_additions=cents(summary["this_additions"]),
        co_this_deductions=cents(summary["this_deductions"]),
    )
    lines = [
        LineView(
            item_no=row["item_no"],
            description=row["description"],
            csi_code=row.get("csi_code", ""),
            c=cents(row["c"]),
            d=cents(row["d"]),
            e=cents(row["e"]),
            f=cents(row["f"]),
            g=cents(row["g"]),
            h=cents(row["h"]),
            i=cents(row["i"]),
            percent_bp=Bp(row["percent_bp"]),
        )
        for row in payload["lines"]
    ]
    rates = (Bp(rule.get("rate_work_bp", 0)), Bp(rule.get("rate_stored_bp", 0)))
    return header, lines, rates


def as_json(view: ApplicationView) -> dict[str, Any]:
    """The JSON payload -- also the webhook body and the API representation."""
    return {
        "application": {
            "number": view.number,
            "period_start": view.period_start.isoformat(),
            "period_end": view.period_end.isoformat(),
            "application_date": view.application_date.isoformat(),
            "status": view.status,
            "from_snapshot": view.from_snapshot,
        },
        "project": {
            "number": view.project_number,
            "name": view.project_name,
            "address": view.project_address,
            "contract_number": view.contract_number,
        },
        "header": {
            f"line{name}": {
                "cents": getattr(view.header, f"line{name}"),
                "amount": str(to_decimal(cents(getattr(view.header, f"line{name}")))),
            }
            for name in ("1", "2", "3", "4", "5a", "5b", "5", "6", "7", "8", "9")
        },
        "change_order_summary": {
            "previous_additions_cents": view.header.co_prev_additions,
            "previous_deductions_cents": view.header.co_prev_deductions,
            "this_period_additions_cents": view.header.co_this_additions,
            "this_period_deductions_cents": view.header.co_this_deductions,
        },
        # Column order matches G703 exactly, so a licence holder can populate
        # their own official document from this (docs/legal-forms-policy.md).
        "lines": [
            {
                "item_no": line.item_no,
                "description": line.description,
                "csi_code": line.csi_code,
                "c_scheduled_value_cents": line.c,
                "d_previous_cents": line.d,
                "e_this_period_cents": line.e,
                "f_stored_cents": line.f,
                "g_completed_stored_cents": line.g,
                "percent_complete_bp": line.percent_bp,
                "h_balance_cents": line.h,
                "i_retainage_cents": line.i,
            }
            for line in view.lines
        ],
        "tieout": {
            "ok": view.tieout_ok,
            "summary": view.tieout_summary,
            "findings": view.findings,
        },
        "disclaimer": view.disclaimer,
    }


def style_for(application: Application) -> FormStyle:
    try:
        return FormStyle(application.form_style)
    except ValueError:
        return FormStyle.AIA_STYLE
