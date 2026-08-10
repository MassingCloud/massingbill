"""The statutory deadline engine, and its refusal to guess.

The refusal tests matter more than the arithmetic ones. A mechanics lien filed
one day late is gone -- no appeal, no cure -- so a day count nobody read out of
the statute is worse than no day count at all. These pin that the engine ships
empty, refuses by citation, and only computes once a human has verified.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from massingbill.extensions import db
from massingbill.models import (
    ClaimantRole,
    DayBasis,
    DeadlineAnchor,
    DeadlineKind,
    DeadlineRule,
)
from massingbill.services import deadlines
from tests.factories import Tenant, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    built.project.jurisdiction_state = "CA"
    deadlines.seed_rules(built.organization)
    db.session.commit()
    return built


def a_rule(tenant: Tenant, kind: DeadlineKind = DeadlineKind.MECHANICS_LIEN) -> DeadlineRule:
    return db.session.scalar(
        db.select(DeadlineRule).where(
            DeadlineRule.organization_id == tenant.organization.id,
            DeadlineRule.state == "CA",
            DeadlineRule.kind == kind,
            DeadlineRule.claimant_role == ClaimantRole.GENERAL_CONTRACTOR,
        )
    )


# ── What ships ──────────────────────────────────────────────────────────────


def test_every_seeded_rule_ships_unverified_with_no_day_count(tenant: Tenant) -> None:
    rules = list(
        db.session.scalars(
            db.select(DeadlineRule).where(DeadlineRule.organization_id == tenant.organization.id)
        )
    )

    assert rules, "the seed installed nothing"
    assert all(r.days is None for r in rules), "a day count shipped unverified"
    assert all(not r.verified for r in rules)
    assert all(not r.is_usable for r in rules)


def test_all_fifty_states_and_dc_are_present(tenant: Tenant) -> None:
    """A contractor whose state is missing would see an empty list and conclude
    they have no obligations."""
    states = {
        r.state
        for r in db.session.scalars(
            db.select(DeadlineRule).where(DeadlineRule.organization_id == tenant.organization.id)
        )
    }
    assert len(states) == 51


def test_seeding_twice_does_not_duplicate(tenant: Tenant) -> None:
    before = len(deadlines.unverified_rules(tenant.organization.id))
    deadlines.seed_rules(tenant.organization)
    db.session.commit()

    assert len(deadlines.unverified_rules(tenant.organization.id)) == before


# ── The refusal ─────────────────────────────────────────────────────────────


def test_an_unverified_rule_refuses_rather_than_computing(tenant: Tenant) -> None:
    obligations = deadlines.compute(tenant.project, on=date(2026, 6, 1))

    assert obligations, "obligations must be listed even when they cannot be computed"
    assert all(not o.is_computable for o in obligations)
    assert all("has not been verified" in o.refusal for o in obligations)


def test_the_refusal_names_what_to_read(tenant: Tenant) -> None:
    """A refusal that does not say what to do next is just an error."""
    obligation = deadlines.compute(tenant.project, on=date(2026, 6, 1))[0]

    assert "enter the number of days" in obligation.refusal
    assert "mark the rule verified" in obligation.refusal


def test_a_verified_rule_with_no_anchor_date_still_refuses(tenant: Tenant) -> None:
    """Verified is not the same as computable."""
    rule = a_rule(tenant)
    deadlines.verify_rule(rule, days=90, citation="Cal. Civ. Code § 8412")
    db.session.commit()

    obligation = next(
        o for o in deadlines.compute(tenant.project, on=date(2026, 6, 1)) if o.rule.id == rule.id
    )
    assert not obligation.is_computable
    assert "has not been recorded" in obligation.refusal


def test_verifying_without_a_citation_is_refused(tenant: Tenant) -> None:
    with pytest.raises(ValueError, match="citation"):
        deadlines.verify_rule(a_rule(tenant), days=90, citation="   ")


def test_a_negative_day_count_is_refused(tenant: Tenant) -> None:
    with pytest.raises(ValueError, match="negative"):
        deadlines.verify_rule(a_rule(tenant), days=-1, citation="somewhere")


# ── The arithmetic ──────────────────────────────────────────────────────────


def test_a_verified_rule_computes_from_its_anchor(tenant: Tenant) -> None:
    rule = a_rule(tenant)
    deadlines.verify_rule(rule, days=90, citation="Cal. Civ. Code § 8412")
    tenant.project.last_furnishing_date = date(2026, 3, 1)
    db.session.commit()

    obligation = next(
        o for o in deadlines.compute(tenant.project, on=date(2026, 3, 2)) if o.rule.id == rule.id
    )
    assert obligation.anchor_date == date(2026, 3, 1)
    assert obligation.due_on == date(2026, 5, 30)


def test_business_days_skip_weekends() -> None:
    # Friday 2026-03-06 plus three business days is Wednesday 2026-03-11.
    assert deadlines.add_days(date(2026, 3, 6), 3, DayBasis.BUSINESS) == date(2026, 3, 11)


def test_calendar_days_do_not() -> None:
    assert deadlines.add_days(date(2026, 3, 6), 3, DayBasis.CALENDAR) == date(2026, 3, 9)


def test_holidays_are_not_modelled_and_that_is_deliberate() -> None:
    """A holiday calendar wrong by one day is worse than an absent one, because
    an absent one makes somebody check. Pinned so nobody 'fixes' it quietly."""
    # 2026-07-03 is the observed Independence Day holiday and a Friday.
    assert deadlines.add_days(date(2026, 7, 2), 1, DayBasis.BUSINESS) == date(2026, 7, 3)


def test_a_deadline_inside_the_window_is_urgent(tenant: Tenant) -> None:
    rule = a_rule(tenant)
    deadlines.verify_rule(rule, days=90, citation="cite")
    tenant.project.last_furnishing_date = date(2026, 3, 1)
    db.session.commit()

    urgent = deadlines.urgent(tenant.project, on=date(2026, 5, 20))
    assert any(o.rule.id == rule.id for o in urgent)


def test_a_distant_deadline_is_not_urgent(tenant: Tenant) -> None:
    rule = a_rule(tenant)
    deadlines.verify_rule(rule, days=90, citation="cite")
    tenant.project.last_furnishing_date = date(2026, 3, 1)
    db.session.commit()

    assert deadlines.urgent(tenant.project, on=date(2026, 3, 2)) == []


def test_a_passed_deadline_is_reported_as_passed(tenant: Tenant) -> None:
    rule = a_rule(tenant)
    deadlines.verify_rule(rule, days=90, citation="cite")
    tenant.project.last_furnishing_date = date(2026, 3, 1)
    db.session.commit()

    obligation = next(
        o for o in deadlines.compute(tenant.project, on=date(2026, 7, 1)) if o.rule.id == rule.id
    )
    assert obligation.is_past(date(2026, 7, 1))
    assert obligation.days_remaining(date(2026, 7, 1)) < 0


# ── Scoping ─────────────────────────────────────────────────────────────────


def test_rules_are_per_organization(tenant: Tenant, app: Flask) -> None:
    stranger = make_tenant("rival")
    db.session.commit()

    assert deadlines.unverified_rules(stranger.organization.id) == []
    assert deadlines.unverified_rules(tenant.organization.id)


def test_only_the_projects_own_state_is_computed(tenant: Tenant) -> None:
    obligations = deadlines.compute(tenant.project, on=date(2026, 6, 1))
    assert all(o.rule.state == "CA" for o in obligations)


def test_the_claimant_role_selects_the_rule(tenant: Tenant) -> None:
    """A GC in privity with the owner often has different notice obligations
    from a second-tier sub."""
    as_gc = deadlines.compute(
        tenant.project, claimant_role=ClaimantRole.GENERAL_CONTRACTOR, on=date(2026, 6, 1)
    )
    as_sub = deadlines.compute(
        tenant.project, claimant_role=ClaimantRole.SUBCONTRACTOR, on=date(2026, 6, 1)
    )

    assert {o.rule.id for o in as_gc}.isdisjoint({o.rule.id for o in as_sub})


def test_anchors_cover_every_recorded_date(tenant: Tenant) -> None:
    """Each anchor has to map to a real field, or a verified rule silently
    refuses forever."""
    tenant.project.first_furnishing_date = date(2026, 1, 5)
    tenant.project.last_furnishing_date = date(2026, 3, 1)
    tenant.project.notice_of_completion_date = date(2026, 3, 20)
    tenant.contract.substantial_completion_date = date(2026, 3, 15)
    db.session.commit()

    for anchor in DeadlineAnchor:
        assert deadlines._anchor_date(tenant.project, tenant.contract, anchor) is not None, anchor
