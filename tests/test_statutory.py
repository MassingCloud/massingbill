"""The statutory worksheet: getting text in without ever inventing it.

The asymmetry is what these hold. The export can only *ask*; the import can only
*move* what a person supplied. There is no path through this module that
produces statutory content, and a blank cell never becomes an answer.
"""

from __future__ import annotations

import csv
import io

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import DeadlineRule, WaiverTemplate
from massingbill.services import deadlines as deadline_service
from massingbill.services import statutory
from massingbill.services import waivers as waiver_service
from tests.factories import Tenant, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    built.project.jurisdiction_state = "CA"
    waiver_service.seed_templates(built.organization)
    deadline_service.seed_rules(built.organization)
    db.session.commit()
    return built


def rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.lstrip("﻿"))))


# ── The export asks, and only asks ──────────────────────────────────────────


def test_the_worksheet_lists_everything_outstanding(tenant: Tenant) -> None:
    parsed = rows(statutory.export_worksheet(tenant.organization.id))

    kinds = {row["kind"] for row in parsed}
    assert kinds == {"waiver", "deadline"}
    assert len(parsed) == sum(statutory.outstanding(tenant.organization.id))


def test_no_exported_row_contains_statutory_content(tenant: Tenant) -> None:
    """The columns a person must fill are empty, and there is no placeholder.
    A suggestion is the one thing that must not appear on this sheet."""
    for row in rows(statutory.export_worksheet(tenant.organization.id)):
        assert row["verbatim_text"] == ""
        assert row["days"] == ""


def test_the_worksheet_carries_the_citation_to_read(tenant: Tenant) -> None:
    """Otherwise the person filling it in has to go looking for what they are
    meant to be reading."""
    waivers = [
        r for r in rows(statutory.export_worksheet(tenant.organization.id)) if r["kind"] == "waiver"
    ]

    assert waivers
    assert all(row["citation"] for row in waivers)
    assert any("Cal. Civ. Code" in row["citation"] for row in waivers)


def test_it_can_be_narrowed_to_one_state(tenant: Tenant) -> None:
    parsed = rows(statutory.export_worksheet(tenant.organization.id, state="CA"))

    assert parsed
    assert {row["state"] for row in parsed} == {"CA"}


def test_the_sheet_opens_in_excel(tenant: Tenant) -> None:
    """It lands on the desk of whoever fills it in. Without a BOM, Excel reads
    UTF-8 as Latin-1 and mangles every section symbol in a citation."""
    content = statutory.export_worksheet(tenant.organization.id)
    assert content.startswith("﻿")


# ── The import moves, and only moves ────────────────────────────────────────


def worksheet_with(tenant: Tenant, *, text: str = "", days: str = "") -> str:
    """The sheet as a lawyer would hand it back.

    Deadline rules ship with **no citation** -- the seed asserts none, on
    purpose -- so verifying one means supplying the day count *and* the section
    it came from. Filling only ``days`` is refused, which the tests below rely
    on, so this fills both.
    """
    content = statutory.export_worksheet(tenant.organization.id)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=statutory.COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows(content):
        if row["kind"] == "waiver" and text:
            row["verbatim_text"] = text
        if row["kind"] == "deadline" and days:
            row["days"] = days
            row["citation"] = row["citation"] or f"{row['state']} statute, section read"
        writer.writerow(row)
    return out.getvalue()


def test_a_filled_worksheet_verifies_what_was_filled(tenant: Tenant) -> None:
    before_waivers, before_deadlines = statutory.outstanding(tenant.organization.id)

    result = statutory.import_worksheet(
        tenant.organization.id,
        worksheet_with(tenant, text="THE VERBATIM STATUTORY TEXT " * 4, days="90"),
    )
    db.session.commit()

    assert result.verified_waivers == before_waivers
    assert result.verified_deadlines == before_deadlines
    assert statutory.outstanding(tenant.organization.id) == (0, 0)


def test_a_blank_row_is_skipped_rather_than_treated_as_an_answer(
    tenant: Tenant,
) -> None:
    """ "We looked and there is no such requirement" is a real finding, but a
    blank cell cannot be trusted to mean it."""
    before = statutory.outstanding(tenant.organization.id)

    result = statutory.import_worksheet(
        tenant.organization.id, statutory.export_worksheet(tenant.organization.id)
    )
    db.session.commit()

    assert result.verified_waivers == 0
    assert result.verified_deadlines == 0
    assert result.skipped_blank == sum(before)
    assert statutory.outstanding(tenant.organization.id) == before


def test_a_row_for_another_tenant_is_refused(tenant: Tenant, app: Flask) -> None:
    stranger = make_tenant("rival")
    waiver_service.seed_templates(stranger.organization)
    db.session.commit()

    theirs = statutory.export_worksheet(stranger.organization.id)
    filled = theirs.replace(',,,"Enter the exact', ',,"SOMEONE ELSE\'S TEXT " * 9,"Enter the exact')

    result = statutory.import_worksheet(tenant.organization.id, filled)
    db.session.commit()

    assert result.verified_waivers == 0
    assert statutory.outstanding(stranger.organization.id)[0] > 0


def test_a_deadline_without_a_citation_is_refused(tenant: Tenant) -> None:
    """A day count with no source is indistinguishable from a guess later."""
    content = statutory.export_worksheet(tenant.organization.id)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=statutory.COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows(content):
        if row["kind"] == "deadline":
            row["days"] = "90"
            row["citation"] = ""
        writer.writerow(row)

    result = statutory.import_worksheet(tenant.organization.id, out.getvalue())

    assert result.verified_deadlines == 0
    assert result.errors


def test_a_non_numeric_day_count_is_reported_not_swallowed(tenant: Tenant) -> None:
    content = statutory.export_worksheet(tenant.organization.id)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=statutory.COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows(content):
        if row["kind"] == "deadline":
            row["days"] = "ninety"
        writer.writerow(row)

    result = statutory.import_worksheet(tenant.organization.id, out.getvalue())

    assert result.verified_deadlines == 0
    assert result.errors


def test_importing_makes_the_refusals_stop(tenant: Tenant) -> None:
    """End to end: the whole reason the worksheet exists."""
    template = waiver_service.unverified_templates(tenant.organization.id)[0]
    rule = deadline_service.unverified_rules(tenant.organization.id, "CA")[0]

    assert not template.is_usable
    assert not rule.is_usable

    statutory.import_worksheet(
        tenant.organization.id,
        worksheet_with(tenant, text="VERBATIM TEXT FROM THE STATUTE " * 3, days="90"),
    )
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(WaiverTemplate, template.id).is_usable
    assert db.session.get(DeadlineRule, rule.id).is_usable
