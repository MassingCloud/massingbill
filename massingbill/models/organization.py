"""Organizations, memberships and invitations.

The organization is the tenant boundary. Every domain row carries an
``organization_id``, every query is scoped through
``services/rbac.py::scoped``, and the authorization tests probe every resource
type for cross-tenant reads. A billing system that leaks one contractor's
schedule of values to another has no second chance.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, DateTime, TimestampMixin, UuidPrimaryKeyMixin

if TYPE_CHECKING:
    from massingbill.models.user import User


class Role(StrEnum):
    """Who someone is inside one organization.

    The first five are staff of the general contractor -- the paying customer.
    The last two are counterparties the GC invites: they hold scoped,
    single-purpose sessions, never see a dashboard, and consume no seat.
    """

    OWNER = "owner"
    ADMIN = "admin"
    PM = "pm"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"
    EXTERNAL_APPROVER = "external_approver"
    SUB_CONTACT = "sub_contact"

    @property
    def is_internal(self) -> bool:
        return self in INTERNAL_ROLES

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


INTERNAL_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.PM, Role.ACCOUNTANT, Role.VIEWER})

ROLE_LABELS = {
    Role.OWNER: "Owner",
    Role.ADMIN: "Administrator",
    Role.PM: "Project manager",
    Role.ACCOUNTANT: "Project accountant",
    Role.VIEWER: "Viewer",
    Role.EXTERNAL_APPROVER: "Owner / architect",
    Role.SUB_CONTACT: "Subcontractor contact",
}


class Organization(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Organization {self.slug}>"


class Membership(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_membership"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[Role] = mapped_column(String(32), nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Membership {self.user_id} {self.role} in {self.organization_id}>"


class Invitation(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A pending invitation to join an organization.

    Only the token's SHA-256 digest is stored, so a database leak does not yield
    usable invitations.
    """

    __tablename__ = "invitations"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    role: Mapped[Role] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped[Organization] = relationship()
