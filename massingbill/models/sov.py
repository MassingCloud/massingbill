"""The schedule of values.

The SOV is the spine of every pay application: G703 column C comes from
``SovLine.current_scheduled_value_cents``, and G702 line 3 must equal their sum.

**Revisions are immutable once an application references them.** A change order
does not rewrite a line's base value; it writes an adjustment so the history
survives and a submitted application can still be re-rendered exactly as it was
approved.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
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
    from massingbill.models.project import PrimeContract


class SovStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class CostCode(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A CSI MasterFormat division or a contractor's own cost code."""

    __tablename__ = "cost_codes"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_cost_code"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    division: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    is_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CostCode {self.code} {self.title}>"


class ScheduleOfValues(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "schedules_of_values"
    __table_args__ = (UniqueConstraint("prime_contract_id", "revision", name="uq_sov_revision"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prime_contract_id: Mapped[str] = mapped_column(
        ForeignKey("prime_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[SovStatus] = mapped_column(String(32), nullable=False, default=SovStatus.DRAFT)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    prime_contract: Mapped[PrimeContract] = relationship(back_populates="schedules")
    lines: Mapped[list[SovLine]] = relationship(
        back_populates="schedule",
        cascade="all, delete-orphan",
        order_by="SovLine.sort_order",
    )

    @property
    def is_editable(self) -> bool:
        return self.status == SovStatus.DRAFT

    @property
    def total_scheduled_value_cents(self) -> int:
        """G702 line 3 must equal this. Summing cents is exact -- no rounding."""
        return sum(line.current_scheduled_value_cents for line in self.lines)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScheduleOfValues rev {self.revision} ({self.status})>"


class SovLine(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One row of the continuation sheet.

    ``current_scheduled_value_cents`` is G703 column C and is maintained as
    ``base + co_adjustment`` rather than being overwritten, so the original
    contract value of a line is always recoverable.
    """

    __tablename__ = "sov_lines"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("schedules_of_values.id", ondelete="CASCADE"), nullable=False, index=True
    )

    item_no: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    csi_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    cost_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    group: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    base_scheduled_value_cents: Mapped[int] = money_column()
    co_adjustment_cents: Mapped[int] = money_column()
    current_scheduled_value_cents: Mapped[int] = money_column()

    #: Unit-price lines. Quantity is in thousandths so a partial unit is exact.
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    quantity_milli: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Caught by the money-discipline gate: this was an Integer, which would have
    # capped a unit price at $21,474,836.47 and put it outside the one helper
    # that marks a column as money.
    unit_price_cents: Mapped[int | None] = money_column(nullable=True, default=None)

    #: Only meaningful when the contract's retainage mode is VARIABLE_LINE
    #: (G703 column I).
    retainage_rate_bp: Mapped[int | None] = bp_column(nullable=True, default=None)

    is_co_line: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_general_conditions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_allowance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowance_balance_cents: Mapped[int | None] = money_column(nullable=True, default=None)

    source_change_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    schedule: Mapped[ScheduleOfValues] = relationship(back_populates="lines")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SovLine {self.item_no} {self.description[:30]}>"
