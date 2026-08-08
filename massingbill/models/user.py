"""Users and their credentials.

A user is global; membership binds them to organizations with a role. TOTP
seeds are encrypted at rest (``services/crypto.py``) and are never rendered,
logged or returned by the API -- only the enrolment QR, once, at enrolment time.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, DateTime, TimestampMixin, UuidPrimaryKeyMixin, utcnow

if TYPE_CHECKING:
    from massingbill.models.organization import Membership


class User(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Two-factor. `totp_secret` holds ciphertext, never the seed.
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lockout. Counting failures and refusing for a window is what makes an
    # argon2 hash worth having -- otherwise an attacker just tries slowly.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # ── Flask-Login interface ───────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    def get_id(self) -> str:
        return self.id

    # ── Domain helpers ──────────────────────────────────────────────────────

    @property
    def mfa_enabled(self) -> bool:
        return self.totp_secret is not None and self.totp_confirmed_at is not None

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()

    @property
    def display_name(self) -> str:
        return self.name or self.email

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email}>"
