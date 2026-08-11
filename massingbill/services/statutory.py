"""Getting statutory text and deadlines *in*, without ever inventing them.

Massing Bill ships prescribed lien-waiver forms with empty bodies and every
deadline rule with no day count, and refuses both until a human enters them.
That refusal is deliberate and is not negotiable: a waiver that does not
substantially conform can be unenforceable, and a mechanics lien filed one day
late is simply gone.

But a refusal that leaves somebody clicking through sixty screens is a refusal
they will route around. So this exports a worksheet of exactly what is
outstanding -- with citations -- and reads the filled worksheet back.

The asymmetry is the point. **Nothing here can produce statutory content**; it
can only move content a person supplied. Every row arrives empty and stays empty
until someone types in it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from massingbill.extensions import db
from massingbill.models import DeadlineRule, User, WaiverTemplate
from massingbill.services import deadlines as deadline_service
from massingbill.services import waivers as waiver_service

#: One sheet covers both kinds, because they are filled by the same person in
#: the same sitting from the same statute.
COLUMNS = (
    "kind",
    "id",
    "state",
    "what",
    "citation",
    "counts_from",
    "days",
    "verbatim_text",
    "notes",
)


@dataclass(frozen=True)
class ImportResult:
    verified_waivers: int = 0
    verified_deadlines: int = 0
    skipped_blank: int = 0
    unknown_rows: int = 0
    errors: list[str] | None = None

    def describe(self) -> str:
        parts = [
            f"{self.verified_waivers} waiver form(s) verified",
            f"{self.verified_deadlines} deadline rule(s) verified",
            f"{self.skipped_blank} row(s) left blank and skipped",
        ]
        if self.unknown_rows:
            parts.append(f"{self.unknown_rows} row(s) referenced nothing recognisable")
        return ", ".join(parts)


def export_worksheet(organization_id: str, *, state: str | None = None) -> str:
    """Every outstanding item, as a CSV a lawyer can fill in.

    Citations are carried through so the person filling it in does not have to
    go looking for what they are meant to be reading.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)

    for template in waiver_service.unverified_templates(organization_id):
        if state and template.state != state:
            continue
        writer.writerow(
            [
                "waiver",
                template.id,
                template.state,
                str(template.waiver_type),
                template.citation,
                "",
                "",
                "",  # verbatim_text -- deliberately empty, no placeholder
                "Enter the exact wording from the citation. Do not paraphrase.",
            ]
        )

    for rule in deadline_service.unverified_rules(organization_id, state):
        writer.writerow(
            [
                "deadline",
                rule.id,
                rule.state,
                f"{rule.kind_label} ({str(rule.claimant_role).replace('_', ' ')})",
                rule.citation,
                str(rule.anchor).replace("_", " "),
                "",  # days -- deliberately empty
                "",
                rule.note or "Enter the day count AND the section you read it from.",
            ]
        )

    # A BOM, because this opens in Excel on the desk of whoever fills it in.
    return "﻿" + buffer.getvalue()


def import_worksheet(
    organization_id: str, content: str, *, actor: User | None = None
) -> ImportResult:
    """Read a filled worksheet back.

    A blank row is skipped rather than treated as an answer -- "we looked and
    there is no such requirement" is a real finding, but it is not one a blank
    cell can be trusted to mean.
    """
    rows = list(csv.DictReader(io.StringIO(content.lstrip("﻿"))))

    verified_waivers = 0
    verified_deadlines = 0
    skipped = 0
    unknown = 0
    errors: list[str] = []

    for index, row in enumerate(rows, start=2):
        kind = (row.get("kind") or "").strip().lower()
        row_id = (row.get("id") or "").strip()

        if kind == "waiver":
            body = (row.get("verbatim_text") or "").strip()
            if not body:
                skipped += 1
                continue
            template = db.session.get(WaiverTemplate, row_id)
            if template is None or template.organization_id != organization_id:
                unknown += 1
                continue
            try:
                waiver_service.verify_template(template, body=body, actor=actor)
                verified_waivers += 1
            except Exception as exc:  # noqa: BLE001 - report the row, keep going
                errors.append(f"row {index} ({template.state}): {exc}")

        elif kind == "deadline":
            raw_days = (row.get("days") or "").strip()
            if not raw_days:
                skipped += 1
                continue
            rule = db.session.get(DeadlineRule, row_id)
            if rule is None or rule.organization_id != organization_id:
                unknown += 1
                continue
            try:
                deadline_service.verify_rule(
                    rule,
                    days=int(raw_days),
                    citation=(row.get("citation") or "").strip(),
                    actor=actor,
                )
                verified_deadlines += 1
            except (ValueError, TypeError) as exc:
                errors.append(f"row {index} ({rule.state} {rule.kind_label}): {exc}")

        elif kind:
            unknown += 1

    return ImportResult(
        verified_waivers=verified_waivers,
        verified_deadlines=verified_deadlines,
        skipped_blank=skipped,
        unknown_rows=unknown,
        errors=errors,
    )


def outstanding(organization_id: str) -> tuple[int, int]:
    """How much is still unverified: (waiver forms, deadline rules)."""
    return (
        len(waiver_service.unverified_templates(organization_id)),
        len(deadline_service.unverified_rules(organization_id)),
    )
