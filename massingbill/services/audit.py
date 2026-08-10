"""Writing and verifying the audit chain.

``record`` appends one event, linked by hash to the previous event in the same
organization. ``verify`` walks a chain and reports the first sequence number
where it breaks.

The chain is only as good as the discipline of writing to it, so ``record``
takes the same shape everywhere: who, what action, which entity, and the before
and after states as plain dictionaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from flask import g, has_request_context, request
from sqlalchemy import func, select

from massingbill.extensions import db
from massingbill.models import GENESIS_HASH, AuditEvent
from massingbill.models.base import utcnow

# Actions. Named as `entity.verb` so a log is greppable by entity or by verb.
ORG_CREATED = "organization.created"
MEMBER_ADDED = "member.added"
MEMBER_ROLE_CHANGED = "member.role_changed"
MEMBER_REMOVED = "member.removed"
USER_REGISTERED = "user.registered"
USER_SIGNED_IN = "user.signed_in"
USER_SIGN_IN_FAILED = "user.sign_in_failed"
USER_SIGNED_OUT = "user.signed_out"
USER_LOCKED = "user.locked"
MFA_ENABLED = "user.mfa_enabled"
MFA_DISABLED = "user.mfa_disabled"
PROJECT_CREATED = "project.created"
PROJECT_UPDATED = "project.updated"
CONTRACT_CREATED = "contract.created"
CONTRACT_UPDATED = "contract.updated"
SOV_CREATED = "sov.created"
SOV_LINE_ADDED = "sov.line_added"
SOV_LINE_UPDATED = "sov.line_updated"
SOV_LINE_REMOVED = "sov.line_removed"
SOV_APPROVED = "sov.approved"
SOV_REVISED = "sov.revised"
APPLICATION_OPENED = "application.opened"
APPLICATION_UPDATED = "application.updated"
APPLICATION_SUBMITTED = "application.submitted"
APPLICATION_CERTIFIED = "application.certified"
APPLICATION_VOIDED = "application.voided"
APPLICATION_PAID = "application.paid"
CO_CREATED = "change_order.created"
CO_APPROVED = "change_order.approved"
CO_VOIDED = "change_order.voided"
STORED_ADDED = "stored_material.added"
STORED_INSTALLED = "stored_material.installed"
WAIVER_REQUESTED = "waiver.requested"
WAIVER_SIGNED = "waiver.signed"
WAIVER_VOIDED = "waiver.voided"
WAIVER_TEMPLATE_VERIFIED = "waiver_template.verified"
COMPLIANCE_REQUIRED = "compliance.required"
COMPLIANCE_FILED = "compliance.filed"
SUBCONTRACT_CREATED = "subcontract.created"
SUB_BILLING_RECEIVED = "sub_billing.received"
SUB_BILLING_APPROVED = "sub_billing.approved"
SUB_BILLING_REJECTED = "sub_billing.rejected"
PAYMENT_RECORDED = "payment.recorded"


def _encode(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def record(
    organization_id: str,
    action: str,
    *,
    entity_type: str = "",
    entity_id: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor_id: str | None = None,
    actor_label: str = "",
) -> AuditEvent:
    """Append one event to an organization's chain.

    Flushes rather than commits: the event lands in the same transaction as the
    change it describes, so a rolled-back edit never leaves an audit entry
    claiming it happened.
    """
    previous = db.session.scalar(
        select(AuditEvent)
        .where(AuditEvent.organization_id == organization_id)
        .order_by(AuditEvent.sequence.desc())
        .limit(1)
    )

    event = AuditEvent(
        organization_id=organization_id,
        sequence=(previous.sequence + 1) if previous else 1,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=_encode(before),
        after=_encode(after),
        request_id=getattr(g, "request_id", "") if has_request_context() else "",
        ip=(request.remote_addr or "") if has_request_context() else "",
        # Truncated to whole seconds so what is stored is exactly what is
        # hashed, whatever the database does with sub-second precision.
        at=utcnow().replace(microsecond=0),
        prev_hash=previous.hash if previous else GENESIS_HASH,
    )
    event.hash = event.compute_hash()

    db.session.add(event)
    db.session.flush()
    return event


def record_for_current_user(organization_id: str, action: str, **kwargs: Any) -> AuditEvent:
    """``record`` with the signed-in user filled in."""
    from flask_login import current_user

    if getattr(current_user, "is_authenticated", False):
        kwargs.setdefault("actor_id", current_user.id)
        kwargs.setdefault("actor_label", current_user.email)
    return record(organization_id, action, **kwargs)


@dataclass(frozen=True)
class ChainVerdict:
    organization_id: str
    events: int
    ok: bool
    broken_at: int | None = None
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return f"{self.organization_id}: {self.events} event(s), chain intact"
        return f"{self.organization_id}: BROKEN at sequence {self.broken_at} -- {self.reason}"


def verify(organization_id: str) -> ChainVerdict:
    """Walk one organization's chain and report the first break."""
    events = list(
        db.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence)
        )
    )

    expected_prev = GENESIS_HASH
    for index, event in enumerate(events, start=1):
        if event.sequence != index:
            return ChainVerdict(
                organization_id,
                len(events),
                ok=False,
                broken_at=event.sequence,
                reason=f"sequence gap: expected {index}, found {event.sequence}",
            )
        if event.prev_hash != expected_prev:
            return ChainVerdict(
                organization_id,
                len(events),
                ok=False,
                broken_at=event.sequence,
                reason="previous-hash link does not match the preceding event",
            )
        if event.hash != event.compute_hash():
            return ChainVerdict(
                organization_id,
                len(events),
                ok=False,
                broken_at=event.sequence,
                reason="event content does not match its recorded hash",
            )
        expected_prev = event.hash

    return ChainVerdict(organization_id, len(events), ok=True)


def verify_all() -> list[ChainVerdict]:
    """Verify every organization's chain."""
    org_ids = list(
        db.session.scalars(
            select(AuditEvent.organization_id)
            .group_by(AuditEvent.organization_id)
            .order_by(func.min(AuditEvent.sequence))
        )
    )
    return [verify(org_id) for org_id in org_ids]
