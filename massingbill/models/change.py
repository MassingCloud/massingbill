"""Change orders and the potential change orders that precede them.

A change order **never rewrites a schedule-of-values line's base value**. It
writes an adjustment, so the original contract value of every line survives and
G702 line 2 can be proved as the sum of approved change orders rather than
merely asserted.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin, money_column


class ChangeOrderType(StrEnum):
    OWNER_CO = "owner_co"
    CCD = "ccd"  # construction change directive
    ALLOWANCE_DRAW = "allowance_draw"
    UNILATERAL = "unilateral"


class ChangeOrderStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    VOID = "void"


class PcoStatus(StrEnum):
    OPEN = "open"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    VOID = "void"


class PotentialChangeOrder(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Priced work that has not yet been executed as a change order."""

    __tablename__ = "potential_change_orders"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prime_contract_id: Mapped[str] = mapped_column(
        ForeignKey("prime_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proposed_amount_cents: Mapped[int] = money_column()
    status: Mapped[PcoStatus] = mapped_column(String(32), nullable=False, default=PcoStatus.OPEN)
    pricing_backup: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ChangeOrder(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """An executed modification to the contract sum."""

    __tablename__ = "change_orders"
    __table_args__ = (
        UniqueConstraint("prime_contract_id", "number", name="uq_change_order_number"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prime_contract_id: Mapped[str] = mapped_column(
        ForeignKey("prime_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_pco_id: Mapped[str | None] = mapped_column(
        ForeignKey("potential_change_orders.id", ondelete="SET NULL"), nullable=True
    )

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    co_type: Mapped[ChangeOrderType] = mapped_column(
        String(32), nullable=False, default=ChangeOrderType.OWNER_CO
    )
    status: Mapped[ChangeOrderStatus] = mapped_column(
        String(32), nullable=False, default=ChangeOrderStatus.DRAFT
    )

    #: Signed. A deductive change order is negative cents -- nothing relies on
    #: a separate flag plus a positive magnitude.
    amount_cents: Mapped[int] = money_column()
    time_extension_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: The period whose "approved this month" box this lands in. Set when the
    #: change order is approved against an open application.
    applies_to_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )

    lines: Mapped[list[ChangeOrderLine]] = relationship(
        back_populates="change_order", cascade="all, delete-orphan"
    )

    @property
    def is_addition(self) -> bool:
        return self.amount_cents >= 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChangeOrder {self.number} {self.amount_cents} ({self.status})>"


class ChangeOrderLine(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """How one change order lands on the schedule of values.

    Either it adjusts an existing line (``sov_line_id`` set) or it creates a new
    one (``new_item_no`` set). Both are recorded; neither overwrites history.
    """

    __tablename__ = "change_order_lines"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_order_id: Mapped[str] = mapped_column(
        ForeignKey("change_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sov_line_id: Mapped[str | None] = mapped_column(
        ForeignKey("sov_lines.id", ondelete="SET NULL"), nullable=True
    )

    new_item_no: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    csi_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    amount_cents: Mapped[int] = money_column()

    change_order: Mapped[ChangeOrder] = relationship(back_populates="lines")

    @property
    def creates_line(self) -> bool:
        return self.sov_line_id is None
