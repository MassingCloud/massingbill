"""Projects, prime contracts and retainage rules.

``Project.jurisdiction_state`` is load-bearing rather than decorative: it selects
the statutory retainage cap and the lien-waiver forms that apply, both of which
are effective-dated data (SPEC.md 0.4). California's SB 61, effective
2026-01-01, is the reason ``is_residential`` and ``stories`` are here -- the 5%
private-works cap excludes residential work unless it is mixed-use or exceeds
four storeys.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.core.enums import RetainageMode
from massingbill.models.base import (
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    bp_column,
    money_column,
)

if TYPE_CHECKING:
    from massingbill.models.sov import ScheduleOfValues


class ProjectStatus(StrEnum):
    PLANNING = "planning"
    ACTIVE = "active"
    CLOSEOUT = "closeout"
    COMPLETE = "complete"
    ARCHIVED = "archived"


#: Defined in the core, because the retainage arithmetic branches on it and the
#: core may not import the ORM. Re-exported here so ``from massingbill.models
#: import RetainageMode`` keeps working and there is exactly one definition -- a
#: second copy would let a persisted value drift from the value the engine tests.
RetainageMode = RetainageMode


class PartyRole(StrEnum):
    OWNER = "owner"
    ARCHITECT = "architect"
    CONTRACTOR = "contractor"
    LENDER = "lender"
    SURETY = "surety"


class FormStyle(StrEnum):
    """Which renderer produces this contract's documents (docs/legal-forms-policy.md)."""

    AIA_STYLE = "aia_style"
    HOUSE = "house"
    CUSTOM = "custom"


class PeriodConvention(StrEnum):
    CALENDAR_MONTH = "calendar_month"
    TWENTY_FIFTH_TO_TWENTY_FOURTH = "25th_to_24th"
    CUSTOM = "custom"


class ContractParty(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """An owner, architect, lender or surety named on a contract."""

    __tablename__ = "contract_parties"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    party_role: Mapped[PartyRole] = mapped_column(String(32), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    contact_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RetainageRule(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """The withholding policy for one prime contract.

    Rates are basis points. ``statutory_cap_bp`` and ``statute_citation`` are
    copied from the effective-dated seed for the project's jurisdiction at the
    time the contract is created, so a later change in the law does not silently
    rewrite the arithmetic of an application that has already been submitted.
    """

    __tablename__ = "retainage_rules"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[RetainageMode] = mapped_column(
        String(32), nullable=False, default=RetainageMode.SPLIT
    )
    rate_work_bp: Mapped[int] = bp_column(default=1000)
    rate_stored_bp: Mapped[int] = bp_column(default=1000)

    #: Stepped mode: once completion reaches this, the rate below applies.
    reduction_threshold_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reduced_rate_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Copied from the jurisdiction seed. `warn` surfaces a tie-out warning,
    #: `block` refuses submission.
    statutory_cap_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    statute_citation: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cap_enforcement: Mapped[str] = mapped_column(String(16), nullable=False, default="warn")


class PrimeContract(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """The contract between the general contractor and the owner."""

    __tablename__ = "prime_contracts"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    original_contract_sum_cents: Mapped[int] = money_column()

    execution_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    substantial_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    period_convention: Mapped[PeriodConvention] = mapped_column(
        String(32), nullable=False, default=PeriodConvention.CALENDAR_MONTH
    )
    billing_day_of_month: Mapped[int] = mapped_column(Integer, nullable=False, default=25)

    retainage_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("retainage_rules.id", ondelete="SET NULL"), nullable=True
    )

    stored_materials_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    offsite_stored_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bonding_required_for_stored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    default_form_style: Mapped[FormStyle] = mapped_column(
        String(32), nullable=False, default=FormStyle.AIA_STYLE
    )

    project: Mapped[Project] = relationship(back_populates="prime_contract")
    retainage_rule: Mapped[RetainageRule | None] = relationship()
    schedules: Mapped[list[ScheduleOfValues]] = relationship(
        back_populates="prime_contract", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PrimeContract {self.number or self.id}>"


class Project(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "number", name="uq_project_number"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: Two-letter state code. Selects the statutory retainage cap and the
    #: lien-waiver forms that govern this project.
    jurisdiction_state: Mapped[str] = mapped_column(String(2), nullable=False, default="")
    is_public_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_residential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stories: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: The events statutory deadlines count from (``services/deadlines.py``).
    #: Kept on the project rather than derived from the applications: first
    #: furnishing is often earlier than the first billing period, and last
    #: furnishing is a fact about the work rather than about the paperwork.
    first_furnishing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_furnishing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_of_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[ProjectStatus] = mapped_column(
        String(32), nullable=False, default=ProjectStatus.PLANNING
    )

    owner_party_id: Mapped[str | None] = mapped_column(
        ForeignKey("contract_parties.id", ondelete="SET NULL"), nullable=True
    )
    architect_party_id: Mapped[str | None] = mapped_column(
        ForeignKey("contract_parties.id", ondelete="SET NULL"), nullable=True
    )

    prime_contract: Mapped[PrimeContract | None] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    owner_party: Mapped[ContractParty | None] = relationship(foreign_keys=[owner_party_id])
    architect_party: Mapped[ContractParty | None] = relationship(foreign_keys=[architect_party_id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project {self.number} {self.name}>"
