"""Subcontracts and the billings that come in against them.

The mirror image of the prime side. A GC bills the owner and is billed by its
subs, and the two halves share the same arithmetic -- so this reuses the money
kernel and the tie-out vocabulary rather than inventing a second one.

**The sub portal is an intake surface, not a product sold to subs.** The paying
customer is the general contractor (SPEC.md 14). Sub contacts hold scoped
magic-link sessions, consume no seat, and are never charged -- which is the
whole of the positioning in docs/competitive-upgrades.md 3.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import (
    Base,
    DateTime,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    bp_column,
    money_column,
)

if TYPE_CHECKING:
    from massingbill.models.project import Project


class SubcontractStatus(StrEnum):
    DRAFT = "draft"
    EXECUTED = "executed"
    COMPLETE = "complete"
    TERMINATED = "terminated"


class SubApplicationStatus(StrEnum):
    """Where a sub's billing stands.

    Deliberately parallel to ``ApplicationStatus`` but not the same enum: a sub
    billing is *received* and *approved* rather than submitted and certified,
    and conflating the two would make the audit log ambiguous about which side
    of the contract an event describes.
    """

    DRAFT = "draft"
    RECEIVED = "received"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    VOID = "void"


class Subcontract(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A commitment from the general contractor to a subcontractor."""

    __tablename__ = "subcontracts"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_subcontract_number"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    number: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    csi_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    original_amount_cents: Mapped[int] = money_column()
    co_adjustment_cents: Mapped[int] = money_column()

    #: Subs are usually held at the same rate the owner holds the GC, but not
    #: always -- a bonded sub may be held at less.
    retainage_rate_bp: Mapped[int] = bp_column(default=1000)

    status: Mapped[SubcontractStatus] = mapped_column(
        String(32), nullable=False, default=SubcontractStatus.DRAFT
    )
    executed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Who to email. No account, no seat, no charge.
    contact_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    contact_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")

    project: Mapped[Project] = relationship()
    lines: Mapped[list[SubcontractLine]] = relationship(
        back_populates="subcontract",
        cascade="all, delete-orphan",
        order_by="SubcontractLine.sort_order",
    )
    applications: Mapped[list[SubApplication]] = relationship(
        back_populates="subcontract",
        cascade="all, delete-orphan",
        order_by="SubApplication.number",
    )

    @property
    def current_amount_cents(self) -> int:
        """Derived, like the prime side: base plus adjustments, never overwritten."""
        return self.original_amount_cents + self.co_adjustment_cents

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Subcontract {self.number} {self.vendor_name}>"


class SubcontractLine(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A sub's own schedule of values."""

    __tablename__ = "subcontract_lines"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subcontract_id: Mapped[str] = mapped_column(
        ForeignKey("subcontracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Which prime SOV line this rolls up into, so a sub billing can be traced
    #: to the line the GC bills the owner on.
    sov_line_id: Mapped[str | None] = mapped_column(
        ForeignKey("sov_lines.id", ondelete="SET NULL"), nullable=True
    )

    item_no: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scheduled_value_cents: Mapped[int] = money_column()

    subcontract: Mapped[Subcontract] = relationship(back_populates="lines")


class SubApplication(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One billing received from a subcontractor."""

    __tablename__ = "sub_applications"
    __table_args__ = (
        UniqueConstraint("subcontract_id", "number", name="uq_sub_application_number"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subcontract_id: Mapped[str] = mapped_column(
        ForeignKey("subcontracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The prime application this sub billing was rolled into, if any.
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True
    )

    number: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SubApplicationStatus] = mapped_column(
        String(32), nullable=False, default=SubApplicationStatus.DRAFT
    )

    #: The same shape as the prime header, so one set of arithmetic serves both.
    completed_to_date_cents: Mapped[int] = money_column()
    stored_cents: Mapped[int] = money_column()
    retainage_cents: Mapped[int] = money_column()
    previous_payments_cents: Mapped[int] = money_column()
    payment_due_cents: Mapped[int] = money_column()

    #: What the GC actually approved, which may be less than was billed.
    approved_amount_cents: Mapped[int | None] = money_column(nullable=True, default=None)
    approval_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    subcontract: Mapped[Subcontract] = relationship(back_populates="applications")

    @property
    def approved_or_billed_cents(self) -> int:
        """What actually moves. Mirrors the prime side's certified-or-requested."""
        if self.approved_amount_cents is not None:
            return self.approved_amount_cents
        return self.payment_due_cents

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SubApplication #{self.number} ({self.status})>"
