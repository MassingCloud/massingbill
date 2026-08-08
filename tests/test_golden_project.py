"""The golden project: SPEC.md 7.2, and the P3 acceptance criterion.

One twelve-month project carrying every hard case, with the expected G702
header **hand-computed and written down** for each period. If the engine and
these numbers disagree, the build fails -- and one of them is wrong on purpose,
so a reviewer can check the engine rather than trusting it.

The project: a $1,200,000.00 contract on four lines. Round numbers, because a
reviewer has to be able to verify every figure below with a calculator and no
special knowledge.

    001  Site work        $200,000.00
    002  Structure        $500,000.00
    003  Envelope         $300,000.00
    004  Fit-out          $200,000.00
                        ------------
                        $1,200,000.00

Retainage: 10% on completed work, 5% on stored material (G702 lines 5a/5b).

The cases, period by period:

    1   first billing, nothing carried forward
    2   ordinary progress
    3   +$120,000.00 owner change order adding a line
    4   $80,000.00 of stored material appears in column F
    5   that material is installed -- it must leave F and enter E, once
    6   -$30,000.00 deductive change order
    7   retainage steps 10% -> 5% at 50% complete
    8   the architect certifies $25,000.00 less than requested
    9   line 7 must follow the certificate, not the request
    10  final period: everything complete, retainage released
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import RetainageMode, Role, StoredMaterial
from massingbill.services import application as app_service
from massingbill.services import change_order as co_service
from massingbill.services import sov as sov_service
from massingbill.services import tieout
from massingbill.services.money import cents, to_display
from tests.factories import Tenant, make_tenant

CONTRACT_SUM = 1_200_000_00

LINES = [
    ("001", "Site work", 200_000_00),
    ("002", "Structure", 500_000_00),
    ("003", "Envelope", 300_000_00),
    ("004", "Fit-out", 200_000_00),
]


@dataclass(frozen=True)
class Expected:
    """The hand-computed G702 header for one period."""

    line3_contract_sum: int
    line4_completed_stored: int
    line5a_work: int
    line5b_stored: int
    line6_earned: int
    line7_previous: int
    line8_due: int
    line9_balance: int

    def assert_matches(self, application, label: str) -> None:
        actual = {
            "line 3 contract sum to date": application.line3_contract_sum_to_date,
            "line 4 completed and stored": application.line4_completed_stored,
            "line 5a retainage on work": application.line5a_retainage_work,
            "line 5b retainage on stored": application.line5b_retainage_stored,
            "line 6 earned less retainage": application.line6_earned_less_retainage,
            "line 7 previous certificates": application.line7_previous_certificates,
            "line 8 current payment due": application.line8_current_payment_due,
            "line 9 balance to finish": application.line9_balance_to_finish,
        }
        expected = {
            "line 3 contract sum to date": self.line3_contract_sum,
            "line 4 completed and stored": self.line4_completed_stored,
            "line 5a retainage on work": self.line5a_work,
            "line 5b retainage on stored": self.line5b_stored,
            "line 6 earned less retainage": self.line6_earned,
            "line 7 previous certificates": self.line7_previous,
            "line 8 current payment due": self.line8_due,
            "line 9 balance to finish": self.line9_balance,
        }

        problems = [
            f"    {name}: expected {to_display(cents(want))}, got {to_display(cents(actual[name]))}"
            for name, want in expected.items()
            if actual[name] != want
        ]
        assert not problems, f"{label} does not match the hand-computed header:\n" + "\n".join(
            problems
        )


@pytest.fixture
def project(app: Flask) -> Tenant:
    tenant = make_tenant("golden", contract_sum_cents=CONTRACT_SUM)
    for item, description, value in LINES:
        sov_service.add_line(
            tenant.schedule,
            sov_service.LineInput(
                item_no=item, description=description, scheduled_value_cents=cents(value)
            ),
            actor=tenant.user(Role.OWNER),
        )

    rule = tenant.contract.retainage_rule
    rule.mode = RetainageMode.SPLIT
    rule.rate_work_bp = 1000  # 10% on work
    rule.rate_stored_bp = 500  # 5% on stored material

    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()
    return tenant


# ── Helpers ─────────────────────────────────────────────────────────────────


def _bill(tenant: Tenant, month: int, entries: list[tuple[int, int]]):
    """Open a period, enter it, and return it."""
    application = app_service.open_period(
        tenant.contract,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        actor=tenant.user(Role.OWNER),
    )
    app_service.enter(
        application,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(work), stored=cents(stored))
            for line, (work, stored) in zip(application.lines, entries, strict=True)
        ],
    )
    return application


def _issue(tenant: Tenant, application) -> None:
    app_service.submit(application, actor=tenant.user(Role.OWNER))
    db.session.commit()


def _revise(tenant: Tenant):
    schedule = sov_service.approved_schedule(tenant.contract)
    return sov_service.create_revision(schedule, actor=tenant.user(Role.OWNER))


# ── The twelve months ───────────────────────────────────────────────────────


def test_the_golden_project_reproduces_every_hand_computed_value(project: Tenant) -> None:
    tenant = project
    owner = tenant.user(Role.OWNER)

    # ── Period 1 ────────────────────────────────────────────────────────────
    # Billed: 60,000 site + 40,000 structure = 100,000.
    # Retainage 10% of 100,000 = 10,000. Earned 90,000. Nothing before it.
    one = _bill(tenant, 1, [(60_000_00, 0), (40_000_00, 0), (0, 0), (0, 0)])
    Expected(
        line3_contract_sum=1_200_000_00,
        line4_completed_stored=100_000_00,
        line5a_work=10_000_00,
        line5b_stored=0,
        line6_earned=90_000_00,
        line7_previous=0,
        line8_due=90_000_00,
        line9_balance=1_110_000_00,
    ).assert_matches(one, "Application 1")
    _issue(tenant, one)

    # ── Period 2 ────────────────────────────────────────────────────────────
    # Billed: +40,000 site, +110,000 structure. To date 250,000.
    # Retainage 25,000. Earned 225,000. Less previous 90,000 = 135,000 due.
    two = _bill(tenant, 2, [(40_000_00, 0), (110_000_00, 0), (0, 0), (0, 0)])
    assert [line.col_d_previous for line in two.lines] == [60_000_00, 40_000_00, 0, 0]
    Expected(
        line3_contract_sum=1_200_000_00,
        line4_completed_stored=250_000_00,
        line5a_work=25_000_00,
        line5b_stored=0,
        line6_earned=225_000_00,
        line7_previous=90_000_00,
        line8_due=135_000_00,
        line9_balance=975_000_00,
    ).assert_matches(two, "Application 2")
    _issue(tenant, two)

    # ── Period 3: a +120,000 change order adding line 005 ───────────────────
    revision = _revise(tenant)
    addition = co_service.create(tenant.contract, number="CO-001", description="Rooftop plant")
    co_service.add_line(
        addition, amount=cents(120_000_00), new_item_no="005", description="Rooftop plant"
    )
    co_service.approve(addition, revision, approved_date=date(2026, 3, 10))
    sov_service.approve(revision, actor=owner)
    db.session.commit()

    # Contract sum to date 1,320,000. Billed +150,000 structure. To date 400,000.
    # Retainage 40,000. Earned 360,000. Less previous 225,000 = 135,000 due.
    three = _bill(tenant, 3, [(0, 0), (150_000_00, 0), (0, 0), (0, 0), (0, 0)])
    Expected(
        line3_contract_sum=1_320_000_00,
        line4_completed_stored=400_000_00,
        line5a_work=40_000_00,
        line5b_stored=0,
        line6_earned=360_000_00,
        line7_previous=225_000_00,
        line8_due=135_000_00,
        line9_balance=960_000_00,
    ).assert_matches(three, "Application 3")
    assert three.line2_net_co == 120_000_00
    assert three.co_summary_this_additions == 120_000_00
    _issue(tenant, three)

    # ── Period 4: 80,000 of stored material on the envelope line ────────────
    schedule = sov_service.approved_schedule(tenant.contract)
    envelope = next(line for line in schedule.lines if line.item_no == "003")
    curtain_wall = StoredMaterial(
        organization_id=tenant.organization.id,
        sov_line_id=envelope.id,
        description="Curtain wall units",
        value_cents=80_000_00,
        invoice_ref="INV-4471",
        supplier="Kawneer",
    )
    db.session.add(curtain_wall)
    db.session.flush()

    # Billed +100,000 structure, plus 80,000 stored.
    # To date 400,000 + 100,000 + 80,000 = 580,000.
    # 5a = 10% of 500,000 work = 50,000. 5b = 5% of 80,000 stored = 4,000.
    # Earned 580,000 - 54,000 = 526,000. Less previous 360,000 = 166,000 due.
    four = _bill(tenant, 4, [(0, 0), (100_000_00, 0), (0, 0), (0, 0), (0, 0)])
    app_service.apply_stored_materials(four)

    assert four.lines[2].col_f_stored == 80_000_00
    Expected(
        line3_contract_sum=1_320_000_00,
        line4_completed_stored=580_000_00,
        line5a_work=50_000_00,
        line5b_stored=4_000_00,
        line6_earned=526_000_00,
        line7_previous=360_000_00,
        line8_due=166_000_00,
        line9_balance=794_000_00,
    ).assert_matches(four, "Application 4")
    _issue(tenant, four)

    # ── Period 5: the stored material is installed ──────────────────────────
    # The trap: it must leave column F and enter column E, and the line's
    # total to date must not move by more than the new work.
    five = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 28),
        actor=owner,
    )
    app_service.install_material(curtain_wall, five)
    app_service.enter(
        five,
        [
            app_service.PeriodEntry(line_id=line.id, this_period=cents(work), stored=cents(0))
            for line, work in zip(five.lines, [0, 0, 80_000_00, 0, 0], strict=True)
        ],
    )
    app_service.apply_stored_materials(five)

    envelope_line = five.lines[2]
    assert envelope_line.col_f_stored == 0, "installed material must leave column F"
    assert envelope_line.col_e_this_period == 80_000_00
    assert envelope_line.col_g_completed_stored == 80_000_00, "billed once, not twice"

    # To date still 580,000 -- but now all of it is work, so 5a = 58,000, 5b = 0.
    # Earned 522,000. Less previous 526,000 = -4,000: the owner is owed money
    # back, because retainage on the material rose from 5% to 10% on installation.
    Expected(
        line3_contract_sum=1_320_000_00,
        line4_completed_stored=580_000_00,
        line5a_work=58_000_00,
        line5b_stored=0,
        line6_earned=522_000_00,
        line7_previous=526_000_00,
        line8_due=-4_000_00,
        line9_balance=798_000_00,
    ).assert_matches(five, "Application 5")
    _issue(tenant, five)

    # ── Period 6: a -30,000 deductive change order ──────────────────────────
    revision = _revise(tenant)
    fit_out = next(line for line in revision.lines if line.item_no == "004")
    deduction = co_service.create(
        tenant.contract, number="CO-002", description="Fit-out scope removed"
    )
    co_service.add_line(deduction, amount=cents(-30_000_00), sov_line=fit_out)
    co_service.approve(deduction, revision, approved_date=date(2026, 6, 10))
    sov_service.approve(revision, actor=owner)
    db.session.commit()

    # Contract sum to date 1,320,000 - 30,000 = 1,290,000.
    # Billed +120,000 structure. To date 700,000. 5a = 70,000. Earned 630,000.
    # Less previous 522,000 = 108,000 due.
    six = _bill(tenant, 6, [(0, 0), (120_000_00, 0), (0, 0), (0, 0), (0, 0)])
    Expected(
        line3_contract_sum=1_290_000_00,
        line4_completed_stored=700_000_00,
        line5a_work=70_000_00,
        line5b_stored=0,
        line6_earned=630_000_00,
        line7_previous=522_000_00,
        line8_due=108_000_00,
        line9_balance=660_000_00,
    ).assert_matches(six, "Application 6")
    assert six.line2_net_co == 90_000_00  # +120,000 - 30,000
    assert six.co_summary_this_deductions == 30_000_00
    _issue(tenant, six)

    # ── Period 7: retainage steps down at 50% complete ──────────────────────
    rule = tenant.contract.retainage_rule
    rule.mode = RetainageMode.STEPPED
    rule.reduction_threshold_bp = 5000
    rule.reduced_rate_bp = 500
    db.session.flush()

    # Billed +30,000 structure. To date 730,000 of 1,290,000 = 56.59% complete,
    # so the reduced 5% applies to the whole balance.
    # 5a = 5% of 730,000 = 36,500. Earned 693,500.
    # Less previous 630,000 = 63,500 due.
    seven = _bill(tenant, 7, [(0, 0), (30_000_00, 0), (0, 0), (0, 0), (0, 0)])
    Expected(
        line3_contract_sum=1_290_000_00,
        line4_completed_stored=730_000_00,
        line5a_work=36_500_00,
        line5b_stored=0,
        line6_earned=693_500_00,
        line7_previous=630_000_00,
        line8_due=63_500_00,
        line9_balance=596_500_00,
    ).assert_matches(seven, "Application 7")
    _issue(tenant, seven)

    # ── Period 8: the architect certifies 25,000 less ───────────────────────
    # Billed +170,000 across envelope and fit-out. To date 900,000.
    # 5a = 5% of 900,000 = 45,000. Earned 855,000.
    # Less previous 693,500 = 161,500 requested.
    eight = _bill(tenant, 8, [(0, 0), (0, 0), (120_000_00, 0), (50_000_00, 0), (0, 0)])
    Expected(
        line3_contract_sum=1_290_000_00,
        line4_completed_stored=900_000_00,
        line5a_work=45_000_00,
        line5b_stored=0,
        line6_earned=855_000_00,
        line7_previous=693_500_00,
        line8_due=161_500_00,
        line9_balance=435_000_00,
    ).assert_matches(eight, "Application 8")
    _issue(tenant, eight)

    certification = app_service.certify(
        eight,
        cents(136_500_00),  # 25,000 less than requested
        certified_by_label="Ferris & Partners, Architects",
        reason="Envelope line not accepted as complete",
    )
    db.session.commit()
    assert certification.variance_cents == -25_000_00

    # ── Period 9: line 7 must follow the certificate ────────────────────────
    # The certificate was 136,500, so previous certificates total
    # 693,500 + 136,500 = 830,000 -- not the 855,000 that was requested.
    nine = _bill(tenant, 9, [(0, 0), (0, 0), (100_000_00, 0), (0, 0), (0, 0)])
    assert nine.line7_previous_certificates == 830_000_00, (
        "line 7 must follow the certificate, not the request -- otherwise the "
        "contractor bills the certified shortfall twice"
    )

    # To date 1,000,000. 5a = 50,000. Earned 950,000. Less 830,000 = 120,000 due.
    Expected(
        line3_contract_sum=1_290_000_00,
        line4_completed_stored=1_000_000_00,
        line5a_work=50_000_00,
        line5b_stored=0,
        line6_earned=950_000_00,
        line7_previous=830_000_00,
        line8_due=120_000_00,
        line9_balance=340_000_00,
    ).assert_matches(nine, "Application 9")
    _issue(tenant, nine)

    # ── Period 10: final. Everything complete, retainage released ───────────
    # Bill the remaining 290,000 so column G equals column C on every line.
    schedule = sov_service.approved_schedule(tenant.contract)
    ten = app_service.open_period(
        tenant.contract,
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 28),
        actor=owner,
    )
    app_service.enter(
        ten,
        [
            app_service.PeriodEntry(
                line_id=line.id,
                this_period=cents(line.col_c_scheduled_value - line.col_d_previous),
                stored=cents(0),
            )
            for line in ten.lines
        ],
    )

    assert ten.line4_completed_stored == 1_290_000_00, "every line billed in full"
    assert all(line.col_h_balance == 0 for line in ten.lines)

    # Release retainage: the rate goes to zero on the final application.
    rule.mode = RetainageMode.FLAT
    rule.rate_work_bp = 0
    rule.rate_stored_bp = 0
    db.session.flush()
    app_service.recompute(ten)

    Expected(
        line3_contract_sum=1_290_000_00,
        line4_completed_stored=1_290_000_00,
        line5a_work=0,
        line5b_stored=0,
        line6_earned=1_290_000_00,
        line7_previous=950_000_00,
        line8_due=340_000_00,
        line9_balance=0,
    ).assert_matches(ten, "Application 10 (final)")
    _issue(tenant, ten)

    assert schedule is not None


# ── Invariants across the whole run ─────────────────────────────────────────


def test_every_period_ties_out(project: Tenant) -> None:
    """Every application in the golden run passes the tie-out engine.

    The arithmetic being right and the *checks* agreeing that it is right are
    two different claims, and this asserts the second.
    """
    tenant = project
    test_the_golden_project_reproduces_every_hand_computed_value(tenant)

    applications = app_service.applications_for(tenant.contract)
    assert len(applications) == 10

    for application in applications:
        report = tieout.run(application)
        assert report.ok, f"Application #{application.number} failed tie-out: " + "; ".join(
            f"{f.rule_id} {f.message}" for f in report.blocking
        )


def test_the_payments_sum_to_the_contract(project: Tenant) -> None:
    """Across the whole project, what was billed equals what was earned.

    The sum of every line 8 must equal the final line 6 -- and with retainage
    fully released, the final line 6 is the contract sum to date. If those
    disagree, money went missing between periods.
    """
    tenant = project
    test_the_golden_project_reproduces_every_hand_computed_value(tenant)

    applications = app_service.applications_for(tenant.contract)
    final = applications[-1]

    total_paid = sum(
        a.certified_or_requested_cents - a.line7_previous_certificates for a in applications
    )

    assert total_paid == final.line6_earned_less_retainage
    assert final.line6_earned_less_retainage == final.line3_contract_sum_to_date
    assert final.line9_balance_to_finish == 0


def test_work_completed_to_date_never_goes_backwards(project: Tenant) -> None:
    """Line 4 is cumulative, so it can only rise.

    Note that **line 9 is not monotonic**, and the golden run proves it:
    between applications 4 and 5 the balance to finish *rises*, because
    installing stored material moves it from the 5% stored rate to the 10% work
    rate, so more is retained and less is earned. That is correct, and it is
    the kind of movement an owner queries -- which is why the tie-out engine
    reports period-on-period movement as an informational finding rather than
    treating it as an error.
    """
    tenant = project
    test_the_golden_project_reproduces_every_hand_computed_value(tenant)

    applications = app_service.applications_for(tenant.contract)
    for previous, current in pairwise(applications):
        assert current.line4_completed_stored >= previous.line4_completed_stored, (
            f"completed and stored to date fell between #{previous.number} and #{current.number}"
        )
        for line in current.lines:
            assert line.col_g_completed_stored >= 0


def test_every_submitted_period_kept_its_snapshot(project: Tenant) -> None:
    tenant = project
    test_the_golden_project_reproduces_every_hand_computed_value(tenant)

    for application in app_service.applications_for(tenant.contract):
        assert application.snapshot is not None, f"#{application.number} has no snapshot"
        assert len(application.snapshot.sha256) == 64
