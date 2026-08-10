"""Compliance documents.

Certificates of insurance, certified payroll, W-9s, bonds. Two things make this
worth modelling rather than filing in a folder:

**They expire.** A COI that lapsed in March is worth nothing in April, and the
person who notices is usually the owner, at the worst moment.

**They can block payment.** A GC who has agreed to collect certified payroll
before releasing funds needs the system to refuse, not to remind. So a
requirement carries ``blocks_payment``, and the tie-out engine turns a missing
or expired document into a finding that stops the application.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin, utcnow


class ComplianceKind(StrEnum):
    CERTIFICATE_OF_INSURANCE = "certificate_of_insurance"
    CERTIFIED_PAYROLL = "certified_payroll"
    W9 = "w9"
    PAYMENT_BOND = "payment_bond"
    PERFORMANCE_BOND = "performance_bond"
    SAFETY_PLAN = "safety_plan"
    LICENSE = "license"
    OTHER = "other"

    @property
    def label(self) -> str:
        return COMPLIANCE_LABELS[self]


COMPLIANCE_LABELS = {
    ComplianceKind.CERTIFICATE_OF_INSURANCE: "Certificate of insurance",
    ComplianceKind.CERTIFIED_PAYROLL: "Certified payroll",
    ComplianceKind.W9: "Form W-9",
    ComplianceKind.PAYMENT_BOND: "Payment bond",
    ComplianceKind.PERFORMANCE_BOND: "Performance bond",
    ComplianceKind.SAFETY_PLAN: "Safety plan",
    ComplianceKind.LICENSE: "Licence",
    ComplianceKind.OTHER: "Other document",
}


class ComplianceRequirement(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """What a project (or a subcontract) demands before it will pay."""

    __tablename__ = "compliance_requirements"
    __table_args__ = (
        UniqueConstraint("project_id", "subcontract_id", "kind", name="uq_compliance_requirement"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null means the requirement applies to the prime contractor.
    subcontract_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    kind: Mapped[ComplianceKind] = mapped_column(String(48), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: Refuse to submit, rather than merely warn.
    blocks_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Certified payroll is due every period; a W-9 is due once.
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Warn this many days before expiry, so it can be chased before it bites.
    warn_days_before_expiry: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    documents: Mapped[list[ComplianceDoc]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ComplianceRequirement {self.kind}>"


class ComplianceDoc(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A document filed against a requirement."""

    __tablename__ = "compliance_docs"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("compliance_requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Set for a recurring requirement, so one period's certified payroll does
    #: not satisfy the next.
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_void: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    requirement: Mapped[ComplianceRequirement] = relationship(back_populates="documents")

    def is_current(self, on: date | None = None) -> bool:
        """Valid on a given date -- the period end, not today.

        Checking against today would fail an application for last quarter
        because a certificate has lapsed since, which is the wrong answer: the
        question is whether cover was in place when the work was done.
        """
        when = on or utcnow().date()
        if self.is_void:
            return False
        if self.effective_from is not None and when < self.effective_from:
            return False
        return not (self.expires_on is not None and self.expires_on < when)

    def days_until_expiry(self, on: date | None = None) -> int | None:
        if self.expires_on is None:
            return None
        return (self.expires_on - (on or utcnow().date())).days

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ComplianceDoc {self.filename or self.id}>"
