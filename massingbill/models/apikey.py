"""API keys and webhook subscriptions -- the machine-facing edge of the product.

Both tables exist to honour massing conventions that cost nothing now and would
be expensive to retrofit (SPEC.md 3.1): ``Authorization: Bearer <key>`` with an
``X-Api-Key`` alternative, and hex HMAC-SHA256 webhook signatures in
``X-Massing-Signature``. A standalone customer points webhooks at their own ERP;
massing.cloud later becomes one more subscriber row, with no new code path.

**The secret is never stored.** Only a SHA-256 digest of it is, alongside a
non-secret ``public_id`` used to find the row. Two consequences are deliberate:
a database disclosure does not yield usable keys, and a lost key cannot be
recovered, only rotated.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import (
    Base,
    DateTime,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    utcnow,
)


class WebhookEvent(StrEnum):
    """The events a subscriber may ask for.

    Fixed vocabulary, shared byte-for-byte with massing (SPEC.md 3.1) so the
    eventual adapter subscribes to names it already knows.
    """

    APPLICATION_SUBMITTED = "application.submitted"
    APPLICATION_CERTIFIED = "application.certified"
    APPLICATION_PAID = "application.paid"
    WAIVER_SIGNED = "waiver.signed"
    CO_APPROVED = "co.approved"
    TIEOUT_FAILED = "tieout.failed"


ALL_WEBHOOK_EVENTS = frozenset(WebhookEvent)


class ApiKey(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """An organization-scoped machine credential."""

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("public_id", name="uq_api_keys_public_id"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The half of the token that is safe to store, log and display. Indexed,
    #: because it is how a presented token finds its row before any comparison
    #: happens -- looking a key up by its secret would mean either an index on
    #: the secret or a table scan comparing every row.
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: SHA-256 of the secret half. A fast hash is the right choice *here*: the
    #: secret is 256 bits of CSPRNG output, so there is no dictionary to attack
    #: and nothing for a slow KDF to buy. Passwords are argon2id because they
    #: are human-chosen; this is not.
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    #: Space-separated permission strings from ``services/rbac.py``. The same
    #: vocabulary as roles, so an API key can never express an authority the
    #: role system has no word for.
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: Observability, and the only way to answer "is this key still in use?"
    #: before revoking it. Written at most once a minute (see the service).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Requests per minute for this key alone. 0 means "use the app default".
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    organization: Mapped[object] = relationship("Organization", viewonly=True)

    @property
    def scope_set(self) -> frozenset[str]:
        return frozenset(self.scopes.split())

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utcnow()

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired

    @property
    def masked(self) -> str:
        """What the UI and the audit log may show. Never the secret."""
        return f"mbil_{self.public_id}_..."


class WebhookSubscription(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Where to POST events, and what to sign them with."""

    __tablename__ = "webhook_subscriptions"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    url: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    #: Space-separated :class:`WebhookEvent` values. Empty means every event.
    events: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: The HMAC key, encrypted at rest with the app's encryption key -- the same
    #: treatment TOTP seeds get, and for the same reason: a subscriber who can
    #: no longer verify our signature has silently lost their integrity check.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    #: Set when deliveries have failed enough times to stop trying. A dead
    #: endpoint that is retried forever is how a queue turns into an outage.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        "WebhookDelivery",
        back_populates="subscription",
        cascade="all, delete-orphan",
        order_by="WebhookDelivery.created_at.desc()",
    )

    @property
    def event_set(self) -> frozenset[WebhookEvent]:
        if not self.events.strip():
            return ALL_WEBHOOK_EVENTS
        return frozenset(WebhookEvent(name) for name in self.events.split())

    def wants(self, event: WebhookEvent) -> bool:
        return self.is_active and self.disabled_at is None and event in self.event_set


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    #: Retries exhausted. Kept rather than deleted: "we tried and gave up" is a
    #: different fact from "we never had it", and only one of them is a bug.
    ABANDONED = "abandoned"


class WebhookDelivery(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One attempt-tracked event for one subscriber.

    The delivery log is the product feature, not the plumbing. "Did you send it?"
    is the first question in every integration dispute, and an answer of "the
    code would have" has never once settled one.
    """

    __tablename__ = "webhook_deliveries"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    event: Mapped[WebhookEvent] = mapped_column(String(64), nullable=False, index=True)

    #: The exact bytes that were signed. Stored so a signature dispute can be
    #: settled against what was actually sent, not against a re-serialization
    #: that may order its keys differently.
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    status: Mapped[DeliveryStatus] = mapped_column(
        String(16), nullable=False, default=DeliveryStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    subscription: Mapped[WebhookSubscription] = relationship(
        "WebhookSubscription", back_populates="deliveries"
    )
