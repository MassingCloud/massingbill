"""The tamper-evident audit log.

Every mutation writes an event whose hash covers its own content *and* the hash
of the event before it, per organization. Altering or removing a past event
breaks the chain from that point forward, and ``massingbill audit verify`` says
exactly where.

Chains are per-organization rather than global so one tenant's activity cannot
be inferred from another's sequence numbers, and so a tenant export is
self-verifying.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from massingbill.models.base import Base, DateTime, UuidPrimaryKeyMixin

#: The first event in an organization's chain links to this.
GENESIS_HASH = "0" * 64


def canonical_timestamp(value: datetime) -> str:
    """A timestamp representation that survives any database round-trip.

    The hash must be recomputable from a row read back out of storage, and
    storage is not faithful: SQLite drops the timezone entirely, and drivers
    differ on sub-second precision. Both would silently break every chain on a
    fresh install -- verification would fail on data nobody had touched.

    So the digest covers whole seconds, normalised to UTC, with a naive value
    read as UTC (which is what it was written as). Second granularity is ample
    for an audit trail: ``sequence`` already orders events within a second.
    """
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditEvent(UuidPrimaryKeyMixin, Base):
    """One recorded action. Append-only: never updated, never deleted."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_org_sequence", "organization_id", "sequence", unique=True),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(254), nullable=False, default="")

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    before: Mapped[str] = mapped_column(Text, nullable=False, default="")
    after: Mapped[str] = mapped_column(Text, nullable=False, default="")

    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    def payload(self) -> dict[str, Any]:
        """The exact fields the hash covers, in a fixed order."""
        return {
            "organization_id": self.organization_id,
            "sequence": self.sequence,
            "actor_id": self.actor_id or "",
            "actor_label": self.actor_label,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "before": self.before,
            "after": self.after,
            "request_id": self.request_id,
            "ip": self.ip,
            "at": canonical_timestamp(self.at),
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        # sort_keys and a compact separator so the digest does not depend on
        # dict ordering or on how json.dumps happens to space things.
        encoded = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditEvent #{self.sequence} {self.action}>"
