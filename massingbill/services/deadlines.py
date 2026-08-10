"""Computing statutory deadlines, and refusing to guess them.

The engine is the deliverable; the day counts are not. Every rule ships
unverified with ``days`` null, and :func:`compute` returns a **refusal** rather
than a date until somebody reads the statute and enters the number. That is the
same discipline the statutory waiver forms follow, and it matters more here:
a waiver signed on a wrong form might still be argued about, while a mechanics
lien filed one day late is simply gone.

What this does *not* do is file, serve or record anything. It computes dates and
warns about them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from massingbill.extensions import db
from massingbill.models import (
    ClaimantRole,
    DayBasis,
    DeadlineAnchor,
    DeadlineKind,
    DeadlineRule,
    Organization,
    PrimeContract,
    Project,
    User,
)
from massingbill.services import audit

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed" / "deadlines.yaml"

#: How close is close enough to shout about. Anything inside this window is a
#: warning on the application, not merely a line on a list.
WARN_WITHIN_DAYS = 30


@dataclass(frozen=True)
class Obligation:
    """One computed deadline, or one refusal to compute it."""

    rule: DeadlineRule
    anchor_date: date | None
    due_on: date | None
    refusal: str = ""

    @property
    def is_computable(self) -> bool:
        return self.due_on is not None

    def days_remaining(self, today: date) -> int | None:
        if self.due_on is None:
            return None
        return (self.due_on - today).days

    def is_urgent(self, today: date) -> bool:
        remaining = self.days_remaining(today)
        return remaining is not None and remaining <= WARN_WITHIN_DAYS

    def is_past(self, today: date) -> bool:
        remaining = self.days_remaining(today)
        return remaining is not None and remaining < 0


# ── Seeding ─────────────────────────────────────────────────────────────────


def seed_rules(organization: Organization) -> int:
    """Install the rule skeleton for an organization.

    Every row lands unverified with no day count. Seeding *structure* is
    useful -- it tells a contractor which obligations exist in their state and
    what each counts from -- and seeding numbers would not be.
    """
    with SEED_PATH.open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle)

    defaults = data["defaults"]
    existing = {
        (rule.state, str(rule.kind), str(rule.claimant_role))
        for rule in db.session.scalars(
            db.select(DeadlineRule).where(DeadlineRule.organization_id == organization.id)
        )
    }

    created = 0
    for entry in data["states"]:
        state = entry["state"]
        for obligation in entry.get("obligations", defaults["obligations"]):
            for role in obligation.get("claimant_roles", defaults["claimant_roles"]):
                key = (state, obligation["kind"], role)
                if key in existing:
                    continue
                db.session.add(
                    DeadlineRule(
                        organization_id=organization.id,
                        state=state,
                        kind=DeadlineKind(obligation["kind"]),
                        claimant_role=ClaimantRole(role),
                        anchor=DeadlineAnchor(obligation["anchor"]),
                        days=None,
                        day_basis=DayBasis(obligation.get("day_basis", "calendar")),
                        citation=entry.get("citation", ""),
                        note=entry.get("note", ""),
                        effective_from=date.fromisoformat(
                            entry.get("effective_from", defaults["effective_from"])
                        ),
                        verified=False,
                    )
                )
                created += 1

    db.session.flush()
    return created


def verify_rule(
    rule: DeadlineRule,
    *,
    days: int,
    citation: str,
    anchor: DeadlineAnchor | None = None,
    day_basis: DayBasis | None = None,
    actor: User | None = None,
) -> None:
    """Record a day count somebody actually read out of the statute."""
    if days < 0:
        raise ValueError("A deadline cannot be a negative number of days.")
    if not citation.strip():
        raise ValueError("Record the citation the day count came from.")

    rule.days = days
    rule.citation = citation.strip()
    if anchor is not None:
        rule.anchor = anchor
    if day_basis is not None:
        rule.day_basis = day_basis
    rule.verified = True
    rule.verified_by_id = actor.id if actor else None

    audit.record(
        rule.organization_id,
        "deadline.rule_verified",
        entity_type="deadline_rule",
        entity_id=rule.id,
        after={"state": rule.state, "kind": str(rule.kind), "days": days, "citation": citation},
        actor_id=actor.id if actor else None,
    )


# ── Computing ───────────────────────────────────────────────────────────────


def _anchor_date(
    project: Project, contract: PrimeContract | None, anchor: DeadlineAnchor
) -> date | None:
    return {
        DeadlineAnchor.FIRST_FURNISHING: project.first_furnishing_date,
        DeadlineAnchor.LAST_FURNISHING: project.last_furnishing_date,
        DeadlineAnchor.NOTICE_OF_COMPLETION: project.notice_of_completion_date,
        DeadlineAnchor.SUBSTANTIAL_COMPLETION: (
            contract.substantial_completion_date if contract else None
        ),
    }[DeadlineAnchor(anchor)]


def add_days(start: date, days: int, basis: DayBasis) -> date:
    """Add calendar or business days.

    Business days here means "not Saturday or Sunday". Public holidays are
    deliberately not modelled: they differ by state and by year, and a holiday
    calendar that is wrong by one day is worse than one that is obviously
    absent -- the second makes somebody check.
    """
    if DayBasis(basis) == DayBasis.CALENDAR:
        return start + timedelta(days=days)

    moved = start
    remaining = days
    while remaining > 0:
        moved += timedelta(days=1)
        if moved.weekday() < 5:
            remaining -= 1
    return moved


def rules_for(organization_id: str, state: str, *, on: date | None = None) -> list[DeadlineRule]:
    """Every rule in force for a state on a date."""
    effective = on or date.today()
    return list(
        db.session.scalars(
            db.select(DeadlineRule)
            .where(
                DeadlineRule.organization_id == organization_id,
                DeadlineRule.state == state,
                DeadlineRule.effective_from <= effective,
            )
            .order_by(DeadlineRule.kind, DeadlineRule.claimant_role)
        )
    )


def compute(
    project: Project,
    *,
    claimant_role: ClaimantRole = ClaimantRole.GENERAL_CONTRACTOR,
    on: date | None = None,
) -> list[Obligation]:
    """Every obligation for this project, computed or refused.

    A refusal is returned as an :class:`Obligation` with no date and a reason,
    rather than omitted. An obligation missing from a list reads as "you have
    none"; an obligation that says "nobody has verified this rule" reads as
    what it is.
    """
    contract = project.prime_contract
    obligations: list[Obligation] = []

    for rule in rules_for(project.organization_id, project.jurisdiction_state, on=on):
        if ClaimantRole(rule.claimant_role) != claimant_role:
            continue

        if not rule.is_usable:
            obligations.append(
                Obligation(
                    rule=rule,
                    anchor_date=None,
                    due_on=None,
                    refusal=(
                        f"The {rule.state} rule for {rule.kind_label.lower()} has not been "
                        f"verified, so no date can be computed. Read "
                        f"{rule.citation or 'the governing statute'}, enter the number of days, "
                        "and mark the rule verified. Massing Bill ships these rules empty on "
                        "purpose: a mechanics lien filed one day late is gone, and a plausible "
                        "number nobody checked is how that happens."
                    ),
                )
            )
            continue

        anchor_date = _anchor_date(project, contract, rule.anchor)
        if anchor_date is None:
            obligations.append(
                Obligation(
                    rule=rule,
                    anchor_date=None,
                    due_on=None,
                    refusal=(
                        f"This deadline counts from {str(rule.anchor).replace('_', ' ')}, "
                        "which has not been recorded for this project yet."
                    ),
                )
            )
            continue

        obligations.append(
            Obligation(
                rule=rule,
                anchor_date=anchor_date,
                due_on=add_days(anchor_date, rule.days or 0, rule.day_basis),
            )
        )

    return obligations


def urgent(project: Project, *, on: date | None = None) -> list[Obligation]:
    """Obligations inside the warning window, or already past."""
    today = on or date.today()
    return [o for o in compute(project, on=on) if o.is_computable and o.is_urgent(today)]


def unverified_rules(organization_id: str, state: str | None = None) -> list[DeadlineRule]:
    query = db.select(DeadlineRule).where(
        DeadlineRule.organization_id == organization_id,
        DeadlineRule.verified.is_(False),
    )
    if state:
        query = query.where(DeadlineRule.state == state)
    return list(db.session.scalars(query.order_by(DeadlineRule.state, DeadlineRule.kind)))
