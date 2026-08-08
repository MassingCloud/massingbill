"""The audit chain: it records, and it detects tampering."""

from __future__ import annotations

from itertools import pairwise

import pytest
from flask import Flask
from sqlalchemy import select

from massingbill.extensions import db
from massingbill.models import GENESIS_HASH, AuditEvent, Role
from massingbill.services import accounts, audit
from tests.factories import Tenant, add_balanced_lines, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return make_tenant("acme")


def _events(tenant: Tenant) -> list[AuditEvent]:
    return list(
        db.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == tenant.organization.id)
            .order_by(AuditEvent.sequence)
        )
    )


# ── Recording ───────────────────────────────────────────────────────────────


def test_creating_a_tenant_writes_a_chain(tenant: Tenant) -> None:
    events = _events(tenant)
    assert len(events) >= 3  # org created, owner added, six more members, sov created

    actions = {event.action for event in events}
    assert audit.ORG_CREATED in actions
    assert audit.MEMBER_ADDED in actions
    assert audit.SOV_CREATED in actions


def test_the_first_event_links_to_genesis(tenant: Tenant) -> None:
    assert _events(tenant)[0].prev_hash == GENESIS_HASH


def test_sequences_are_dense_and_ordered(tenant: Tenant) -> None:
    sequences = [event.sequence for event in _events(tenant)]
    assert sequences == list(range(1, len(sequences) + 1))


def test_each_event_links_to_the_one_before(tenant: Tenant) -> None:
    events = _events(tenant)
    for previous, current in pairwise(events):
        assert current.prev_hash == previous.hash


def test_a_fresh_chain_verifies(tenant: Tenant) -> None:
    verdict = audit.verify(tenant.organization.id)
    assert verdict.ok
    assert verdict.events == len(_events(tenant))
    assert "intact" in verdict.describe()


def test_the_chain_survives_a_database_round_trip(tenant: Tenant) -> None:
    """Regression: the hash covered ``at.isoformat()``, but SQLite drops the
    timezone on the way back out, so every chain failed verification on a fresh
    install -- on data nobody had touched."""
    organization_id = tenant.organization.id
    db.session.expunge_all()  # force every row to be read back from storage

    reloaded = list(
        db.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence)
        )
    )
    assert reloaded, "expected events to reload"
    for event in reloaded:
        assert event.hash == event.compute_hash()

    assert audit.verify(organization_id).ok


def test_chains_are_independent_per_organization(app: Flask) -> None:
    """One tenant's activity must not be inferable from another's sequence."""
    first = make_tenant("acme")
    second = make_tenant("rival")

    assert audit.verify(first.organization.id).ok
    assert audit.verify(second.organization.id).ok
    assert _events(first)[0].sequence == 1
    assert _events(second)[0].sequence == 1


def test_schedule_edits_are_recorded(tenant: Tenant) -> None:
    before = len(_events(tenant))
    add_balanced_lines(tenant, count=3)

    events = _events(tenant)
    assert len(events) == before + 3
    assert all(e.action == audit.SOV_LINE_ADDED for e in events[before:])


def test_an_event_captures_before_and_after(tenant: Tenant) -> None:
    viewer_membership = next(
        m
        for m in accounts.memberships_for(tenant.user(Role.VIEWER))
        if m.organization_id == tenant.organization.id
    )
    accounts.change_member_role(viewer_membership, Role.PM, actor=tenant.user(Role.OWNER))
    db.session.commit()

    event = _events(tenant)[-1]
    assert event.action == audit.MEMBER_ROLE_CHANGED
    assert "viewer" in event.before
    assert "pm" in event.after


# ── Tamper detection ────────────────────────────────────────────────────────


def test_editing_an_event_breaks_the_chain(tenant: Tenant) -> None:
    """The whole point: altering history must be detectable after the fact."""
    events = _events(tenant)
    target = events[1]

    target.action = "organization.definitely_not_what_happened"
    db.session.commit()

    verdict = audit.verify(tenant.organization.id)
    assert not verdict.ok
    assert verdict.broken_at == target.sequence
    assert "does not match its recorded hash" in verdict.reason


def test_deleting_an_event_breaks_the_chain(tenant: Tenant) -> None:
    events = _events(tenant)
    db.session.delete(events[1])
    db.session.commit()

    verdict = audit.verify(tenant.organization.id)
    assert not verdict.ok
    assert "sequence gap" in verdict.reason


def test_relinking_a_forged_event_is_still_caught(tenant: Tenant) -> None:
    """A careful attacker recomputes the hash of the row they changed. The link
    from the *next* event still does not match."""
    events = _events(tenant)
    target = events[1]

    target.actor_label = "someone.else@example.com"
    target.hash = target.compute_hash()  # forge convincingly
    db.session.commit()

    verdict = audit.verify(tenant.organization.id)
    assert not verdict.ok
    assert verdict.broken_at == events[2].sequence
    assert "previous-hash link" in verdict.reason


def test_verify_all_reports_every_organization(app: Flask) -> None:
    make_tenant("acme")
    make_tenant("rival")

    verdicts = audit.verify_all()
    assert len(verdicts) == 2
    assert all(v.ok for v in verdicts)


def test_an_organization_with_no_events_verifies_trivially(app: Flask) -> None:
    verdict = audit.verify("nonexistent-organization")
    assert verdict.ok
    assert verdict.events == 0


def test_a_rolled_back_change_leaves_no_audit_entry(tenant: Tenant) -> None:
    """The event lands in the same transaction as the change it describes, so
    an entry can never claim something happened that did not."""
    before = len(_events(tenant))

    audit.record(
        tenant.organization.id,
        audit.PROJECT_CREATED,
        entity_type="project",
        entity_id="never-committed",
    )
    db.session.rollback()

    assert len(_events(tenant)) == before
