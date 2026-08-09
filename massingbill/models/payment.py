"""Recorded payments.

**Massing Bill does not move money** (SPEC.md 1.2, and the competitive review
confirms it: ACH means money-transmitter licensing in fifty jurisdictions and it
changes what kind of company this is). It records what was received, so that

- ``PAY-VARIANCE`` can compare what was certified against what actually arrived,
- retainage-release forecasting has real dates rather than assumptions,
- and a conditional waiver can be shown to have taken effect, since it does so
  only on payment.

That last one is why this table exists at all. A conditional waiver that nobody
can prove was paid is a waiver nobody can rely on.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin, money_column


class PaymentMethod(StrEnum):
    CHECK = "check"
    ACH = "ach"
    WIRE = "wire"
    JOINT_CHECK = "joint_check"
    OTHER = "other"


class Payment(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Money received against one application."""

    __tablename__ = "payments"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )

    amount_cents: Mapped[int] = money_column()
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        String(32), nullable=False, default=PaymentMethod.CHECK
    )
    reference: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: A joint cheque names a second payee -- usually a supplier the sub owes.
    #: Modelled because the *release* depends on it: both parties' waivers have
    #: to be in hand. We record the arrangement; we do not issue the cheque.
    joint_payee: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    application: Mapped[object] = relationship("Application", viewonly=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Payment {self.amount_cents} on {self.received_on}>"
