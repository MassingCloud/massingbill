"""The tie-out rule engine.

Every competitor produces the forms. None of them hands a general contractor a
**proof that the numbers tie** -- which is what an owner's auditor, a lender's
inspector and a sceptical project accountant actually need. That proof is this
module.

Each rule is a small pure function with an id, a sentence a human can read, a
severity, and a citation to the G702/G703 definition it enforces. The engine
runs on demand, on every save, and **blocking** at submit; the result is
rendered in-app and appended to the document package as a reconciliation page.

Severities:

``ERROR``
    The arithmetic does not hold. Submission is refused -- there is no reading
    of the form under which the application is correct.
``WARNING``
    The arithmetic holds but the result is contestable: overbilling, retainage
    above a statutory cap, unbacked stored material. Submission proceeds, and
    the finding travels with the package so nobody is ambushed by it later.
``INFO``
    Context worth carrying: cash movement against the prior period, retainage
    held to date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from massingbill.extensions import db
from massingbill.models import (
    Application,
    ApplicationStatus,
    ChangeOrder,
    ChangeOrderStatus,
    ComplianceKind,
    RetainageMode,
    StoredMaterial,
)
from massingbill.services import retainage as retainage_service
from massingbill.services.money import cents, percent_of, to_display


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    message: str
    expected: int | None = None
    actual: int | None = None
    citation: str = ""
    line_item: str = ""

    @property
    def delta(self) -> int | None:
        if self.expected is None or self.actual is None:
            return None
        return self.actual - self.expected

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "message": self.message,
            "expected_cents": self.expected,
            "actual_cents": self.actual,
            "delta_cents": self.delta,
            "citation": self.citation,
            "line_item": self.line_item,
        }


@dataclass
class TieoutReport:
    application_id: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def informational(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.INFO]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "Every check passed. The application ties."
        parts = []
        if self.blocking:
            parts.append(f"{len(self.blocking)} blocking issue(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return " and ".join(parts) + "."


def run(application: Application) -> TieoutReport:
    """Run every rule against one application."""
    report = TieoutReport(application_id=application.id)
    findings = report.findings

    findings.extend(_structural(application))
    findings.extend(_line_level(application))
    findings.extend(_policy(application))
    findings.extend(_snapshot(application))
    findings.extend(_informational(application))

    return report


def _snapshot(app: Application) -> list[Finding]:
    """An issued application must still match the snapshot taken when it was.

    This replaces the live-data checks for a frozen record. Rather than asking
    "does this agree with the world today", which it should not have to, it asks
    the question that actually matters: "is this the same document that was
    submitted".
    """
    if app.is_editable or app.status == ApplicationStatus.VOID:
        return []

    if app.snapshot is None:
        return [
            Finding(
                "SNAPSHOT-MISSING",
                Severity.ERROR,
                "This application was issued without a snapshot, so it cannot be shown "
                "to be the document that was submitted.",
                citation="submitted applications are frozen",
            )
        ]

    import hashlib

    from massingbill.services import application as application_service

    current = application_service.frozen_fingerprint(app)
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != app.snapshot.sha256:
        return [
            Finding(
                "SNAPSHOT-DRIFT",
                Severity.ERROR,
                "This application no longer matches the snapshot taken when it was "
                "submitted -- its stored figures have been altered since.",
                citation="submitted applications are frozen",
            )
        ]

    return []


# ── Structural rules: the arithmetic must hold ──────────────────────────────


def _structural(app: Application) -> list[Finding]:
    out: list[Finding] = []

    def check(rule_id: str, expected: int, actual: int, message: str, citation: str) -> None:
        if expected != actual:
            out.append(
                Finding(
                    rule_id=rule_id,
                    severity=Severity.ERROR,
                    message=(
                        f"{message} Expected {to_display(cents(expected))}, "
                        f"found {to_display(cents(actual))}."
                    ),
                    expected=expected,
                    actual=actual,
                    citation=citation,
                )
            )

    lines = app.lines
    contract = app.prime_contract

    # SOV-001: the schedule must total the contract sum to date.
    check(
        "SOV-001",
        app.line3_contract_sum_to_date,
        sum(line.col_c_scheduled_value for line in lines),
        "The schedule of values does not total the contract sum to date.",
        "G703 column C sums to G702 line 3",
    )

    # G702-002: line 2 is the sum of approved change orders.
    #
    # Only meaningful while the application is still open. A submitted
    # application is a frozen record of a moment, and the change-order log keeps
    # moving after it -- so comparing a five-month-old application against
    # today's log would report a break on a record that was correct when issued
    # and has not been touched since. Frozen applications are verified against
    # their own snapshot instead (see ``_snapshot``).
    if _checks_live_data(app):
        approved = (
            db.session.scalars(
                select(ChangeOrder).where(
                    ChangeOrder.prime_contract_id == contract.id,
                    ChangeOrder.status == ChangeOrderStatus.APPROVED,
                )
            )
            if contract is not None
            else []
        )
        check(
            "G702-002",
            sum(co.amount_cents for co in approved),
            app.line2_net_co,
            "Line 2 does not equal the sum of approved change orders.",
            "G702 line 2",
        )

    check(
        "G702-003",
        app.line1_original_sum + app.line2_net_co,
        app.line3_contract_sum_to_date,
        "Line 3 does not equal line 1 plus line 2.",
        "G702 line 3 = 1 + 2",
    )
    check(
        "G702-004",
        sum(line.col_g_completed_stored for line in lines),
        app.line4_completed_stored,
        "Line 4 does not equal the total of column G.",
        "G702 line 4 = sum of G703 column G",
    )
    check(
        "G702-005",
        app.line5a_retainage_work + app.line5b_retainage_stored,
        app.line5_total_retainage,
        "Total retainage does not equal 5a plus 5b.",
        "G702 line 5 = 5a + 5b",
    )
    check(
        "G702-006",
        app.line4_completed_stored - app.line5_total_retainage,
        app.line6_earned_less_retainage,
        "Line 6 does not equal line 4 less line 5.",
        "G702 line 6 = 4 - 5",
    )

    # G702-007: line 7 is the cumulative certified total, not the prior request.
    if _checks_live_data(app):
        from massingbill.services import application as application_service

        previous = (
            application_service.previous_issued(contract, app.number)
            if contract is not None
            else None
        )
        check(
            "G702-007",
            previous.certified_or_requested_cents if previous else 0,
            app.line7_previous_certificates,
            "Line 7 does not equal the previous certificates for payment.",
            "G702 line 7 = line 6 from the prior certificate",
        )

    check(
        "G702-008",
        app.line6_earned_less_retainage - app.line7_previous_certificates,
        app.line8_current_payment_due,
        "Line 8 does not equal line 6 less line 7.",
        "G702 line 8 = 6 - 7",
    )
    check(
        "G702-009",
        app.line3_contract_sum_to_date - app.line6_earned_less_retainage,
        app.line9_balance_to_finish,
        "Line 9 does not equal line 3 less line 6.",
        "G702 line 9 = 3 - 6",
    )

    # CO-SUM: the change-order box must net to line 2.
    box_net = (
        app.co_summary_prev_additions
        - app.co_summary_prev_deductions
        + app.co_summary_this_additions
        - app.co_summary_this_deductions
    )
    check(
        "CO-SUM",
        app.line2_net_co,
        box_net,
        "The change-order summary box does not net to line 2.",
        "G702 change order summary",
    )

    # PENNY: the per-line retainage must sum to the header, to the cent.
    check(
        "PENNY",
        app.line5_total_retainage,
        sum(line.col_i_retainage for line in lines),
        "Per-line retainage does not sum to the header retainage.",
        "G703 column I sums to G702 line 5",
    )

    return out


# ── Line-level rules ────────────────────────────────────────────────────────


def _checks_live_data(app: Application) -> bool:
    """Whether cross-record checks against live tables are meaningful.

    They are, right up until the application is issued. After that it is a
    financial record and the world moves on around it: the change-order log
    grows, the schedule of values is revised, later periods are certified.
    Re-checking a frozen document against a moved world reports breaks in
    records that were correct when issued and have not been touched since.
    """
    return app.is_editable


def _line_level(app: Application) -> list[Finding]:
    out: list[Finding] = []

    from massingbill.services import application as application_service

    previous = (
        application_service.previous_issued(app.prime_contract, app.number)
        if app.prime_contract is not None and _checks_live_data(app)
        else None
    )
    # Keyed by item number, matching the period engine: a schedule revision
    # gives every line a new id, so an id-keyed comparison would report a false
    # break on the first application after any change order.
    prior_by_item = (
        {line.item_no: line.carry_forward_cents for line in previous.lines} if previous else {}
    )

    for line in app.lines:
        expected_g = line.col_d_previous + line.col_e_this_period + line.col_f_stored
        if expected_g != line.col_g_completed_stored:
            out.append(
                Finding(
                    "G703-G",
                    Severity.ERROR,
                    f"Line {line.item_no}: column G does not equal D + E + F.",
                    expected=expected_g,
                    actual=line.col_g_completed_stored,
                    citation="G703 column G = D + E + F",
                    line_item=line.item_no,
                )
            )

        expected_h = line.col_c_scheduled_value - line.col_g_completed_stored
        if expected_h != line.col_h_balance:
            out.append(
                Finding(
                    "G703-H",
                    Severity.ERROR,
                    f"Line {line.item_no}: column H does not equal C - G.",
                    expected=expected_h,
                    actual=line.col_h_balance,
                    citation="G703 column H = C - G",
                    line_item=line.item_no,
                )
            )

        expected_d = prior_by_item.get(line.item_no, 0)
        if _checks_live_data(app) and expected_d != line.col_d_previous:
            out.append(
                Finding(
                    "G703-D",
                    Severity.ERROR,
                    (
                        f"Line {line.item_no}: column D does not match the previous "
                        f"application's D + E."
                    ),
                    expected=expected_d,
                    actual=line.col_d_previous,
                    citation="G703 column D carries forward from the prior application",
                    line_item=line.item_no,
                )
            )

    return out


# ── Policy rules: the arithmetic holds, but the result is contestable ───────


def _policy(app: Application) -> list[Finding]:
    out: list[Finding] = []
    contract = app.prime_contract
    rule = contract.retainage_rule if contract is not None else None

    for line in app.lines:
        if line.col_g_completed_stored > line.col_c_scheduled_value:
            excess = cents(line.col_g_completed_stored - line.col_c_scheduled_value)
            out.append(
                Finding(
                    "OVERBILL",
                    Severity.WARNING,
                    (
                        f"Line {line.item_no} is billed above its scheduled value by "
                        f"{to_display(excess)}."
                    ),
                    expected=line.col_c_scheduled_value,
                    actual=line.col_g_completed_stored,
                    citation="G703 column G should not exceed column C",
                    line_item=line.item_no,
                )
            )

        if line.percent_complete_bp > 10_000:
            out.append(
                Finding(
                    "PCT-OVER",
                    Severity.WARNING,
                    f"Line {line.item_no} reports more than 100% complete.",
                    citation="G703 percent complete = G / C",
                    line_item=line.item_no,
                )
            )

        if line.col_e_this_period < 0:
            out.append(
                Finding(
                    "NEGATIVE-PERIOD",
                    Severity.WARNING,
                    (
                        f"Line {line.item_no} bills a negative amount this period. "
                        "That is only correct alongside a deductive change order."
                    ),
                    actual=line.col_e_this_period,
                    citation="G703 column E",
                    line_item=line.item_no,
                )
            )

    # RETAIN-CAP: the effective withholding against the statutory ceiling.
    if rule is not None and rule.statutory_cap_bp is not None and app.line4_completed_stored:
        effective = percent_of(cents(app.line5_total_retainage), cents(app.line4_completed_stored))
        if effective > rule.statutory_cap_bp:
            citation = rule.statute_citation or "statutory retainage cap"
            out.append(
                Finding(
                    "RETAIN-CAP",
                    Severity.ERROR if rule.cap_enforcement == "block" else Severity.WARNING,
                    (
                        f"Retainage of {effective / 100:.2f}% exceeds the "
                        f"{rule.statutory_cap_bp / 100:.2f}% cap for this jurisdiction "
                        f"({citation})."
                    ),
                    expected=rule.statutory_cap_bp,
                    actual=effective,
                    citation=citation,
                )
            )

    # Stored materials: backup, and the double-bill trap.
    out.extend(_stored_material_rules(app))

    # Waivers, compliance and payment (docs/competitive-upgrades.md U1, U3, U4).
    out.extend(_waiver_rules(app))
    out.extend(_compliance_rules(app))
    out.extend(_payment_rules(app))
    out.extend(_party_rules(app))
    out.extend(_deadline_rules(app))

    # SEQUENCE: gaps in application numbering.
    if contract is not None and _checks_live_data(app):
        from massingbill.services import application as application_service

        previous = application_service.previous_issued(contract, app.number)
        if previous is not None and previous.number != app.number - 1:
            out.append(
                Finding(
                    "SEQUENCE",
                    Severity.WARNING,
                    (
                        f"This is application #{app.number}, but the previous issued "
                        f"application was #{previous.number}."
                    ),
                    citation="applications should be consecutively numbered",
                )
            )

    return out


def _stored_material_rules(app: Application) -> list[Finding]:
    out: list[Finding] = []
    contract = app.prime_contract
    if contract is None:
        return out

    line_ids = [line.sov_line_id for line in app.lines]
    materials = list(
        db.session.scalars(
            select(StoredMaterial).where(
                StoredMaterial.sov_line_id.in_(line_ids), StoredMaterial.is_void.is_(False)
            )
        )
    )
    by_line = {line.sov_line_id: line for line in app.lines}

    for material in materials:
        line = by_line.get(material.sov_line_id)
        item = line.item_no if line else "?"

        if material.is_installed and material.installed_in_application_id == app.id:
            # STORED-DOUBLE: installed material must leave column F. If it is
            # still there, the same material is being billed twice.
            if line is not None and line.col_f_stored >= material.value_cents > 0:
                out.append(
                    Finding(
                        "STORED-DOUBLE",
                        Severity.ERROR,
                        (
                            f"Line {item}: {to_display(cents(material.value_cents))} of material "
                            "was installed this period but is still in column F. It would be "
                            "billed twice."
                        ),
                        actual=material.value_cents,
                        citation="G703 column F excludes anything in D or E",
                        line_item=item,
                    )
                )
            continue

        if line is not None and line.col_f_stored == 0:
            continue

        if not material.has_backup:
            out.append(
                Finding(
                    "STORED-UNBACKED",
                    Severity.WARNING,
                    (
                        f"Line {item}: stored material has no supplier invoice on file. "
                        "Owners routinely refuse to pay for material they cannot verify."
                    ),
                    citation="G703 column F backup",
                    line_item=item,
                )
            )

        if material.is_offsite and not contract.offsite_stored_allowed:
            out.append(
                Finding(
                    "STORED-OFFSITE",
                    Severity.WARNING,
                    (
                        f"Line {item}: material is stored off site, which this contract "
                        "does not permit billing for."
                    ),
                    citation="contract terms for stored materials",
                    line_item=item,
                )
            )
        elif material.is_offsite and contract.bonding_required_for_stored and not material.bond_ref:
            out.append(
                Finding(
                    "STORED-UNBONDED",
                    Severity.WARNING,
                    (
                        f"Line {item}: off-site material has no bond reference, which this "
                        "contract requires."
                    ),
                    citation="contract terms for off-site storage",
                    line_item=item,
                )
            )

    return out


# ── Waivers (competitive-upgrades.md U1) ────────────────────────────────────


def _waiver_rules(app: Application) -> list[Finding]:
    """Handle's "waiver protection safeguards", as tie-out rules.

    A waiver states an amount and a through-date. If either disagrees with the
    payment it releases, lien rights are being given up for work nobody has
    paid for -- and unlike an arithmetic error, that is not discovered when the
    next application is prepared. It is discovered when the money is gone.
    """
    from massingbill.services import waivers as waiver_service

    out: list[Finding] = []
    instances = waiver_service.for_application(app)
    if not instances:
        return out

    for waiver in instances:
        item = f"{waiver.claimant or 'claimant'} ({_waiver_label(waiver)})"

        # WAIVER-AMOUNT: the release must match what is actually being paid.
        expected = app.certified_payment_cents
        if waiver.amount_cents != expected:
            out.append(
                Finding(
                    "WAIVER-AMOUNT",
                    Severity.ERROR if waiver.amount_cents > expected else Severity.WARNING,
                    (
                        f"{item}: the waiver releases {to_display(cents(waiver.amount_cents))} "
                        f"but the payment for this period is "
                        f"{to_display(cents(expected))}."
                        + (
                            " It releases more than is being paid."
                            if waiver.amount_cents > expected
                            else ""
                        )
                    ),
                    expected=expected,
                    actual=waiver.amount_cents,
                    citation="a waiver must release only what is paid",
                    line_item=item,
                )
            )

        # WAIVER-THROUGH-DATE: a through-date before the period end leaves work
        # in this period unreleased; after it releases work not yet billed.
        if waiver.through_date < app.period_start:
            out.append(
                Finding(
                    "WAIVER-THROUGH-DATE",
                    Severity.WARNING,
                    (
                        f"{item}: the waiver runs through "
                        f"{waiver.through_date.isoformat()}, before this period began."
                    ),
                    citation="waiver through-date against the billing period",
                    line_item=item,
                )
            )
        elif waiver.through_date > app.period_end:
            out.append(
                Finding(
                    "WAIVER-THROUGH-DATE",
                    Severity.ERROR,
                    (
                        f"{item}: the waiver runs through "
                        f"{waiver.through_date.isoformat()}, past the end of this period. "
                        "It would release rights for work that has not been billed."
                    ),
                    citation="waiver through-date against the billing period",
                    line_item=item,
                )
            )

        # WAIVER-DETACHED: the document was edited after it was signed.
        if waiver.is_signed and not waiver_service.signature_is_intact(waiver):
            out.append(
                Finding(
                    "WAIVER-DETACHED",
                    Severity.ERROR,
                    (
                        f"{item}: the signature no longer matches the waiver text. "
                        "The document was changed after it was signed."
                    ),
                    citation="the signature binds the exact rendered document",
                    line_item=item,
                )
            )

        # WAIVER-UNCONDITIONAL-EARLY: an unconditional waiver signed before the
        # money arrives is the classic way a contractor loses lien rights.
        if waiver.is_signed and not waiver.is_conditional and app.status != ApplicationStatus.PAID:
            out.append(
                Finding(
                    "WAIVER-UNCONDITIONAL-EARLY",
                    Severity.WARNING,
                    (
                        f"{item}: an unconditional waiver has been signed, but this "
                        "application is not recorded as paid. An unconditional waiver "
                        "takes effect on signature, not on payment."
                    ),
                    citation="conditional waivers take effect on payment",
                    line_item=item,
                )
            )

    return out


def _waiver_label(waiver: Any) -> str:
    """The waiver's type as a phrase.

    Coerced back through the enum because the column is a plain string and
    SQLAlchemy hands it back as one -- the same reason ``Role(...)`` is coerced
    everywhere it is read.
    """
    from massingbill.models import WaiverType

    return WaiverType(waiver.waiver_type).label.lower()


# ── Compliance (competitive-upgrades.md U4) ─────────────────────────────────


def _compliance_rules(app: Application) -> list[Finding]:
    """Missing or lapsed documents, judged as at the period end.

    ``blocks_payment`` decides whether a gap refuses the application or merely
    reports it -- because a GC who has agreed to withhold funds until certified
    payroll is in hand needs a refusal, and one who simply likes having a W-9
    on file needs a nudge.
    """
    from massingbill.services import compliance as compliance_service

    out: list[Finding] = []
    if app.prime_contract is None or app.prime_contract.project is None:
        return out

    for state in compliance_service.evaluate(app):
        # Coerced: the column is a string, so SQLAlchemy returns one.
        label = ComplianceKind(state.requirement.kind).label

        if not state.satisfied:
            out.append(
                Finding(
                    "COMPLIANCE-MISSING",
                    Severity.ERROR if state.requirement.blocks_payment else Severity.WARNING,
                    (
                        f"{label}: {state.reason}."
                        + (
                            " This document is required before payment."
                            if state.requirement.blocks_payment
                            else ""
                        )
                    ),
                    citation="project compliance requirements",
                    line_item=label,
                )
            )
        elif state.expiring_soon:
            out.append(
                Finding(
                    "COMPLIANCE-EXPIRING",
                    Severity.WARNING,
                    (
                        f"{label} expires in {state.expires_in_days} day(s). "
                        "Chase it before the next period."
                    ),
                    citation="project compliance requirements",
                    line_item=label,
                )
            )

    return out


# ── Payment (competitive-upgrades.md U3) ────────────────────────────────────


def _payment_rules(app: Application) -> list[Finding]:
    """What was certified against what actually arrived.

    Reported rather than blocking: a short payment is a fact about the owner,
    not a defect in the application. But it has to be visible, because it is the
    number that decides whether a conditional waiver has taken effect and
    whether the next period's line 7 is what everyone assumes.
    """
    from massingbill.services import payments as payment_service

    if app.is_editable:
        return []

    paid = payment_service.paid_to_date(app)
    if paid == 0:
        return []

    certified = app.certified_payment_cents
    if paid == certified:
        return []

    outstanding = certified - paid
    return [
        Finding(
            "PAY-VARIANCE",
            Severity.WARNING if outstanding > 0 else Severity.INFO,
            (
                f"Certified {to_display(cents(certified))}, received "
                f"{to_display(cents(paid))}"
                + (
                    f" -- {to_display(cents(outstanding))} still outstanding."
                    if outstanding > 0
                    else f" -- {to_display(cents(-outstanding))} more than certified."
                )
            ),
            expected=certified,
            actual=paid,
            citation="recorded payments against the certificate",
        )
    ]


# ── Party details (competitive-upgrades.md U2) ──────────────────────────────


def _party_rules(app: Application) -> list[Finding]:
    """Details a document needs before it goes out.

    Handle validates project data *before* sending; this is the same idea. A
    waiver naming the wrong legal entity, or a project with no address in a
    state whose notice requires one, is unenforceable in a way nobody notices
    until it is needed.
    """
    out: list[Finding] = []
    contract = app.prime_contract
    project = contract.project if contract is not None else None
    if project is None:
        return out

    if not project.jurisdiction_state:
        out.append(
            Finding(
                "PARTY-MISSING",
                Severity.WARNING,
                (
                    "This project has no state on file, so no statutory retainage cap "
                    "or lien-waiver form can be selected for it."
                ),
                citation="project jurisdiction",
            )
        )

    if not project.address.strip():
        out.append(
            Finding(
                "ADDRESS-MISSING",
                Severity.WARNING,
                (
                    "This project has no address on file. Several states require the "
                    "property to be identified on a lien waiver or preliminary notice."
                ),
                citation="project address",
            )
        )

    if contract is not None and not contract.number.strip():
        out.append(
            Finding(
                "PARTY-MISSING",
                Severity.INFO,
                "The prime contract has no contract number recorded.",
                citation="contract identification",
            )
        )

    return out


# ── Informational ───────────────────────────────────────────────────────────


def _informational(app: Application) -> list[Finding]:
    out: list[Finding] = []
    contract = app.prime_contract

    out.append(
        Finding(
            "INFO-RETAINED",
            Severity.INFO,
            f"Retainage held to date: {to_display(cents(app.line5_total_retainage))}.",
            actual=app.line5_total_retainage,
        )
    )
    complete_bp = percent_of(
        cents(app.line4_completed_stored), cents(app.line3_contract_sum_to_date)
    )
    out.append(
        Finding(
            "INFO-COMPLETE",
            Severity.INFO,
            f"Project is {complete_bp / 100:.2f}% complete by value.",
            actual=int(complete_bp),
        )
    )

    if contract is not None:
        from massingbill.services import application as application_service

        previous = application_service.previous_issued(contract, app.number)
        if previous is not None:
            movement = app.line8_current_payment_due - previous.line8_current_payment_due
            out.append(
                Finding(
                    "INFO-MOVEMENT",
                    Severity.INFO,
                    (
                        "Payment due moved "
                        f"{to_display(cents(movement))} against the previous application."
                    ),
                    actual=movement,
                )
            )

        rule = contract.retainage_rule
        if rule is not None and rule.mode == RetainageMode.STEPPED:
            work_rate, _ = retainage_service.effective_rates(
                rule,
                completed_stored=cents(app.line4_completed_stored),
                contract_sum=cents(app.line3_contract_sum_to_date),
            )
            out.append(
                Finding(
                    "INFO-STEP",
                    Severity.INFO,
                    f"Retainage is being withheld at {work_rate / 100:.2f}% this period.",
                    actual=int(work_rate),
                )
            )

    return out


__all__ = ["Finding", "Severity", "TieoutReport", "run"]


def _deadline_rules(app: Application) -> list[Finding]:
    """Statutory deadlines that are close, or already gone.

    On the application rather than only on a project page, because the pay
    application is the document a project accountant opens every month, and a
    lien deadline is the one thing on this list that cannot be fixed after the
    fact.

    Unverified rules are reported too. A missing obligation reads as "you have
    none"; one that says nobody has checked the rule reads as what it is.
    """
    if not _checks_live_data(app):
        return []

    from massingbill.services import deadlines as deadline_service

    contract = app.prime_contract
    project = contract.project if contract else None
    if project is None or not project.jurisdiction_state:
        return []

    today = app.period_end
    out: list[Finding] = []

    for obligation in deadline_service.compute(project, on=today):
        rule = obligation.rule

        if obligation.refusal and not rule.is_usable:
            out.append(
                Finding(
                    "DEADLINE-UNVERIFIED",
                    Severity.WARNING,
                    obligation.refusal,
                    citation=rule.citation or f"{rule.state} statute",
                )
            )
            continue

        if not obligation.is_computable:
            continue

        remaining = obligation.days_remaining(today)
        if remaining is None:
            continue

        if remaining < 0:
            out.append(
                Finding(
                    "DEADLINE-PASSED",
                    Severity.WARNING,
                    (
                        f"{rule.kind_label} in {rule.state} was due "
                        f"{obligation.due_on} — {abs(remaining)} day(s) ago."
                    ),
                    citation=rule.citation,
                )
            )
        elif obligation.is_urgent(today):
            out.append(
                Finding(
                    "DEADLINE-NEAR",
                    Severity.WARNING,
                    (
                        f"{rule.kind_label} in {rule.state} is due {obligation.due_on}, "
                        f"in {remaining} day(s)."
                    ),
                    citation=rule.citation,
                )
            )

    return out
