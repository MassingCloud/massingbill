"""Schedule-of-values behaviour: ties, immutability and revisions."""

from __future__ import annotations

import pytest
from flask import Flask

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import Role, SovStatus
from massingbill.services import sov as sov_service
from massingbill.services.money import cents
from tests.factories import Tenant, add_balanced_lines, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return make_tenant("acme")


def _line(item_no: str, value: int) -> sov_service.LineInput:
    return sov_service.LineInput(
        item_no=item_no, description=f"Work for {item_no}", scheduled_value_cents=cents(value)
    )


# ── Building ────────────────────────────────────────────────────────────────


def test_a_new_schedule_starts_at_revision_one(tenant: Tenant) -> None:
    assert tenant.schedule.revision == 1
    assert tenant.schedule.status == SovStatus.DRAFT
    assert tenant.schedule.is_editable


def test_a_contract_gets_only_one_schedule(tenant: Tenant) -> None:
    with pytest.raises(ConflictError, match="already has a schedule"):
        sov_service.create_schedule(tenant.contract, actor=tenant.user(Role.OWNER))


def test_lines_are_ordered_as_entered(tenant: Tenant) -> None:
    for index in range(1, 4):
        sov_service.add_line(tenant.schedule, _line(f"{index:03d}", 100_000), actor=None)
    db.session.commit()

    assert [line.item_no for line in tenant.schedule.lines] == ["001", "002", "003"]


def test_duplicate_item_numbers_are_refused(tenant: Tenant) -> None:
    sov_service.add_line(tenant.schedule, _line("001", 100_000), actor=None)
    with pytest.raises(ConflictError, match="already used"):
        sov_service.add_line(tenant.schedule, _line("001", 50_000), actor=None)


def test_a_line_needs_a_description(tenant: Tenant) -> None:
    empty = sov_service.LineInput(
        item_no="001", description="   ", scheduled_value_cents=cents(100)
    )
    with pytest.raises(ValidationError, match="needs a description"):
        sov_service.add_line(tenant.schedule, empty, actor=None)


def test_the_current_scheduled_value_starts_at_the_base(tenant: Tenant) -> None:
    line = sov_service.add_line(tenant.schedule, _line("001", 250_000), actor=None)
    assert line.base_scheduled_value_cents == 250_000
    assert line.co_adjustment_cents == 0
    assert line.current_scheduled_value_cents == 250_000


def test_editing_a_line_preserves_its_change_order_adjustment(tenant: Tenant) -> None:
    """Column C is derived. A change order's effect on a line must survive an
    unrelated edit to that line's description or base value."""
    line = sov_service.add_line(tenant.schedule, _line("001", 250_000), actor=None)

    line.co_adjustment_cents = 18_750  # as a change order would have set it
    db.session.flush()

    sov_service.update_line(line, _line("001", 300_000), actor=None)

    assert line.base_scheduled_value_cents == 300_000
    assert line.co_adjustment_cents == 18_750
    assert line.current_scheduled_value_cents == 318_750


# ── Tying to the contract sum ───────────────────────────────────────────────


def test_the_total_is_the_sum_of_the_lines(tenant: Tenant) -> None:
    for index, value in enumerate([500_000, 300_000, 200_000], start=1):
        sov_service.add_line(tenant.schedule, _line(f"{index:03d}", value), actor=None)
    db.session.commit()

    assert tenant.schedule.total_scheduled_value_cents == 1_000_000


def test_reconciliation_reports_the_gap(tenant: Tenant) -> None:
    sov_service.add_line(tenant.schedule, _line("001", 1_000_000), actor=None)
    db.session.commit()

    result = sov_service.reconciliation(tenant.schedule)
    assert result["contract_sum_cents"] == 1_245_000_000
    assert result["lines_total_cents"] == 1_000_000
    assert result["difference_cents"] == 1_000_000 - 1_245_000_000


def test_a_schedule_that_does_not_tie_cannot_be_approved(tenant: Tenant) -> None:
    sov_service.add_line(tenant.schedule, _line("001", 1_000_000), actor=None)
    db.session.commit()

    with pytest.raises(ValidationError, match="does not tie to the contract sum"):
        sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))


def test_an_empty_schedule_cannot_be_approved(tenant: Tenant) -> None:
    with pytest.raises(ValidationError, match="at least one line"):
        sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))


def test_a_balanced_schedule_approves(tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=7)

    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert tenant.schedule.status == SovStatus.APPROVED
    assert tenant.schedule.approved_at is not None
    assert tenant.schedule.approved_by_id == tenant.user(Role.OWNER).id


def test_allocation_makes_an_odd_contract_sum_tie_exactly(app: Flask) -> None:
    """A contract sum that does not divide evenly still balances to the cent --
    the money kernel's largest-remainder allocation doing its job end to end."""
    odd = make_tenant("odd", contract_sum_cents=1_000_003)
    add_balanced_lines(odd, count=7)

    assert sov_service.reconciliation(odd.schedule)["difference_cents"] == 0
    sov_service.approve(odd.schedule, actor=odd.user(Role.OWNER))


# ── Immutability and revisions ──────────────────────────────────────────────


def test_an_approved_schedule_refuses_edits(tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=3)
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    with pytest.raises(ConflictError, match="cannot be edited"):
        sov_service.add_line(tenant.schedule, _line("999", 1), actor=None)

    with pytest.raises(ConflictError, match="cannot be edited"):
        sov_service.update_line(tenant.schedule.lines[0], _line("001", 5), actor=None)

    with pytest.raises(ConflictError, match="cannot be edited"):
        sov_service.remove_line(tenant.schedule.lines[0], actor=None)


def test_a_revision_copies_the_lines_and_supersedes_the_original(tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=4)
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    original_values = [line.current_scheduled_value_cents for line in tenant.schedule.lines]

    revision = sov_service.create_revision(
        tenant.schedule, note="Owner change order 1", actor=tenant.user(Role.OWNER)
    )
    db.session.commit()

    assert revision.revision == 2
    assert revision.status == SovStatus.DRAFT
    assert tenant.schedule.status == SovStatus.SUPERSEDED
    assert [line.current_scheduled_value_cents for line in revision.lines] == original_values


def test_a_superseded_revision_keeps_its_lines(tenant: Tenant) -> None:
    """Applications already built against it must still render exactly."""
    add_balanced_lines(tenant, count=4)
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    before = len(tenant.schedule.lines)
    sov_service.create_revision(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert len(tenant.schedule.lines) == before


def test_only_an_approved_revision_can_be_superseded(tenant: Tenant) -> None:
    with pytest.raises(ConflictError, match="Only an approved revision"):
        sov_service.create_revision(tenant.schedule, actor=tenant.user(Role.OWNER))


def test_current_schedule_returns_the_newest_revision(tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=2)
    sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    revision = sov_service.create_revision(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert sov_service.current_schedule(tenant.contract).id == revision.id


def test_approved_schedule_finds_the_approved_one_not_the_draft(tenant: Tenant) -> None:
    add_balanced_lines(tenant, count=2)
    approved = sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
    db.session.commit()

    assert sov_service.approved_schedule(tenant.contract).id == approved.id


def test_a_change_order_line_cannot_be_deleted(tenant: Tenant) -> None:
    """Deleting it would break the proof that line 2 equals the sum of approved
    change orders."""
    line = sov_service.add_line(tenant.schedule, _line("001", 100_000), actor=None)
    line.is_co_line = True
    db.session.flush()

    with pytest.raises(ConflictError, match="Void the change order"):
        sov_service.remove_line(line, actor=None)


def test_getting_a_line_from_another_schedule_is_not_found(app: Flask) -> None:
    first = make_tenant("acme")
    second = make_tenant("rival")
    add_balanced_lines(second, count=1)

    with pytest.raises(NotFoundError):
        sov_service.get_line(first.schedule, second.schedule.lines[0].id)
