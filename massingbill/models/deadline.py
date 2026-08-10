"""Statutory deadlines: the rules, and what they compute against.

**Massing Bill computes and warns. It does not file, serve or record anything.**
Filing is a regulated service business with per-state requirements, and taking
it on would make this a different company (``docs/competitive-upgrades.md``).

Every rule is effective-dated and carries its citation, and every rule ships
**unverified with no day count**, for the same reason the statutory waiver forms
ship empty: a missed mechanics-lien deadline is unrecoverable. A plausible
number here would be worse than no number, because a contractor would rely on
it. The engine refuses an unverified rule by name and citation rather than
guessing (see ``services/deadlines.py``).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from massingbill.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


class DeadlineKind(StrEnum):
    """The four obligations that actually lose rights when missed."""

    PRELIMINARY_NOTICE = "preliminary_notice"
    NOTICE_OF_INTENT = "notice_of_intent"
    MECHANICS_LIEN = "mechanics_lien"
    SUIT_TO_FORECLOSE = "suit_to_foreclose"

    @property
    def label(self) -> str:
        return DEADLINE_LABELS[self]


DEADLINE_LABELS = {
    DeadlineKind.PRELIMINARY_NOTICE: "Preliminary notice",
    DeadlineKind.NOTICE_OF_INTENT: "Notice of intent to lien",
    DeadlineKind.MECHANICS_LIEN: "Mechanics lien",
    DeadlineKind.SUIT_TO_FORECLOSE: "Suit to foreclose",
}


class DeadlineAnchor(StrEnum):
    """The event a deadline counts from.

    Which anchor applies is itself statutory and varies by state -- several
    states run the lien period from last furnishing, several from completion,
    and a few from whichever comes first. That is part of what has to be
    verified, not assumed.
    """

    FIRST_FURNISHING = "first_furnishing"
    LAST_FURNISHING = "last_furnishing"
    SUBSTANTIAL_COMPLETION = "substantial_completion"
    NOTICE_OF_COMPLETION = "notice_of_completion"


class DayBasis(StrEnum):
    CALENDAR = "calendar"
    BUSINESS = "business"


class ClaimantRole(StrEnum):
    """Deadlines differ by who is claiming.

    A general contractor in privity with the owner often has different (or no)
    preliminary-notice obligations than a second-tier sub or a supplier.
    """

    GENERAL_CONTRACTOR = "general_contractor"
    SUBCONTRACTOR = "subcontractor"
    SUPPLIER = "supplier"


class DeadlineRule(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One state's rule for one obligation, effective from a date."""

    __tablename__ = "deadline_rules"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "state",
            "kind",
            "claimant_role",
            "effective_from",
            name="uq_deadline_rule",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    state: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    kind: Mapped[DeadlineKind] = mapped_column(String(32), nullable=False)
    claimant_role: Mapped[ClaimantRole] = mapped_column(String(32), nullable=False)

    anchor: Mapped[DeadlineAnchor] = mapped_column(String(32), nullable=False)

    #: Nullable on purpose, and null is what ships. A number here that nobody
    #: checked against the statute is the failure this whole model is shaped to
    #: prevent.
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_basis: Mapped[DayBasis] = mapped_column(
        String(16), nullable=False, default=DayBasis.CALENDAR
    )

    citation: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Someone read the statute, entered the day count, and said so.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def is_usable(self) -> bool:
        """A rule may only be computed from when it has been checked."""
        return self.verified and self.days is not None

    @property
    def kind_label(self) -> str:
        """Coerced, because a StrEnum column reads back as ``str`` (see
        ``models/compliance.py``)."""
        return DeadlineKind(self.kind).label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DeadlineRule {self.state} {self.kind}>"
