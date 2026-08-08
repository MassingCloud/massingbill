"""Applications for payment: the G702 header and the G703 grid.

The frozen header fields mirror the G702 line numbering exactly, because that
is what every counterparty reads and what the tie-out engine asserts against.

Two fields are **derived once and never edited**: column D (work completed from
previous applications) and line 7 (less previous certificates). Both come from
the preceding application and are written when the period opens. Letting anyone
type them is how a pay app ends up claiming money that was already paid.
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
    from massingbill.models.project import PrimeContract
    from massingbill.models.sov import ScheduleOfValues


class ApplicationStatus(StrEnum):
    """Where an application is in its life.

    ``DRAFT`` is the only editable state. Everything after it is a financial
    record: ``VOID`` exists because financial records are never deleted.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    SUBMITTED = "submitted"
    CERTIFIED = "certified"
    REJECTED = "rejected"
    PAID = "paid"
    VOID = "void"


#: States in which the numbers may still change.
EDITABLE_STATUSES = frozenset({ApplicationStatus.DRAFT, ApplicationStatus.REJECTED})

#: States that count as "issued" for the purpose of the next period's carry-forward.
ISSUED_STATUSES = frozenset(
    {
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CERTIFIED,
        ApplicationStatus.PAID,
    }
)


class Application(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One monthly requisition -- the G702 cover sheet."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("prime_contract_id", "number", name="uq_application_number"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prime_contract_id: Mapped[str] = mapped_column(
        ForeignKey("prime_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("schedules_of_values.id", ondelete="RESTRICT"), nullable=False
    )

    number: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    application_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[ApplicationStatus] = mapped_column(
        String(32), nullable=False, default=ApplicationStatus.DRAFT
    )
    form_style: Mapped[str] = mapped_column(String(32), nullable=False, default="aia_style")

    # ── The G702 header, frozen at submission ───────────────────────────────
    line1_original_sum: Mapped[int] = money_column()
    line2_net_co: Mapped[int] = money_column()
    line3_contract_sum_to_date: Mapped[int] = money_column()
    line4_completed_stored: Mapped[int] = money_column()
    line5a_retainage_work: Mapped[int] = money_column()
    line5b_retainage_stored: Mapped[int] = money_column()
    line5_total_retainage: Mapped[int] = money_column()
    line6_earned_less_retainage: Mapped[int] = money_column()
    line7_previous_certificates: Mapped[int] = money_column()
    line8_current_payment_due: Mapped[int] = money_column()
    line9_balance_to_finish: Mapped[int] = money_column()

    # ── The change-order summary box ────────────────────────────────────────
    co_summary_prev_additions: Mapped[int] = money_column()
    co_summary_prev_deductions: Mapped[int] = money_column()
    co_summary_this_additions: Mapped[int] = money_column()
    co_summary_this_deductions: Mapped[int] = money_column()

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    prime_contract: Mapped[PrimeContract] = relationship()
    schedule: Mapped[ScheduleOfValues] = relationship()
    lines: Mapped[list[ApplicationLine]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationLine.sort_order",
    )
    certification: Mapped[Certification | None] = relationship(
        back_populates="application", uselist=False, cascade="all, delete-orphan"
    )
    snapshot: Mapped[ApplicationSnapshot | None] = relationship(
        back_populates="application", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def is_issued(self) -> bool:
        return self.status in ISSUED_STATUSES

    @property
    def certified_payment_cents(self) -> int:
        """What was actually certified for payment this period.

        The architect certifies an *amount* -- the current payment, line 8 --
        and attaches an explanation when it differs from what was applied for.
        With no certificate on file the request stands.
        """
        if self.certification is not None:
            return self.certification.amount_certified_cents
        return self.line8_current_payment_due

    @property
    def certified_or_requested_cents(self) -> int:
        """What the next application carries forward as line 7.

        G702 line 7 is "less previous certificates for payment" -- the
        **cumulative** total certified to date, which is this period's line 7
        plus whatever this period's certificate actually allowed.

        With no certificate that reduces to line 6, exactly as the form's
        parenthetical ("Line 6 from prior Certificate") implies. When an
        architect certifies less than was asked for, the difference stays
        outstanding rather than being quietly re-billed: the next period's
        line 8 picks it up again, which is the correct behaviour -- the work is
        still unpaid, and the contractor is entitled to ask again.
        """
        return self.line7_previous_certificates + self.certified_payment_cents

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Application #{self.number} ({self.status})>"


class ApplicationLine(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One row of the G703 continuation sheet for one period."""

    __tablename__ = "application_lines"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sov_line_id: Mapped[str] = mapped_column(
        ForeignKey("sov_lines.id", ondelete="RESTRICT"), nullable=False
    )

    # Copied from the SOV line so the row renders without a join, and so a
    # later SOV revision cannot silently restate a submitted period.
    item_no: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    csi_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── G703 columns ────────────────────────────────────────────────────────
    col_c_scheduled_value: Mapped[int] = money_column()
    col_d_previous: Mapped[int] = money_column()
    col_e_this_period: Mapped[int] = money_column()
    col_f_stored: Mapped[int] = money_column()
    col_g_completed_stored: Mapped[int] = money_column()
    col_h_balance: Mapped[int] = money_column()
    col_i_retainage: Mapped[int] = money_column()
    percent_complete_bp: Mapped[int] = bp_column()

    is_co_line: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    application: Mapped[Application] = relationship(back_populates="lines")

    @property
    def carry_forward_cents(self) -> int:
        """What column D of the next period becomes: this period's D + E."""
        return self.col_d_previous + self.col_e_this_period

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApplicationLine {self.item_no} G={self.col_g_completed_stored}>"


class Certification(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """The architect's or owner's certificate.

    ``amount_certified_cents`` may differ from line 8, and when it does the
    variance is recorded with a reason rather than quietly overwriting the
    request.
    """

    __tablename__ = "certifications"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    amount_certified_cents: Mapped[int] = money_column()
    variance_cents: Mapped[int] = money_column()
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    certified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    certified_by_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    certified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    application: Mapped[Application] = relationship(back_populates="certification")


class ApplicationSnapshot(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A hashed freeze of everything a submitted application depended on.

    The single most important durability decision in the schema: a submitted
    application must re-render byte-identically in five years, after the
    schedule of values has been revised four times and the retainage rate has
    changed. Storing the inputs rather than trusting the live rows is what makes
    that true.
    """

    __tablename__ = "application_snapshots"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    payload: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    application: Mapped[Application] = relationship(back_populates="snapshot")
