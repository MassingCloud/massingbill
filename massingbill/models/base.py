"""Shared model plumbing.

Two decisions here are load-bearing for the whole product and are settled now so
nothing has to be migrated later:

**Money is integer cents in a ``BIGINT``.** Never float, never ``Numeric`` on the
money path. ``money_column`` is the only way a monetary value enters the schema,
which is what makes the discipline greppable (SPEC.md 5).

**Percentages are basis points.** ``10%`` is ``1000``. There is no float percent
anywhere in the system.

Financial records are never hard-deleted -- ``void`` is a state. ``SoftDeleteMixin``
exists for the non-financial rows where deletion is genuinely appropriate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from massingbill.extensions import db

Base = db.Model


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def money_column(*, nullable: bool = False, default: int | None = 0) -> Mapped[int]:
    """Declare a monetary column. Integer cents, ``BIGINT``, nothing else.

    Every monetary value in the schema is declared through this helper so that
    a reviewer -- and the CI money-discipline gate -- can find all of them.
    """
    return mapped_column(BigInteger, nullable=nullable, default=default)


def bp_column(*, nullable: bool = False, default: int | None = 0) -> Mapped[int]:
    """Declare a basis-point column (1 bp = 0.01%). Never a float."""
    return mapped_column(Integer, nullable=nullable, default=default)


class UuidPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    """For non-financial rows only. Financial records use an explicit void state."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
