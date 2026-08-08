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
from typing import Any

from sqlalchemy import BigInteger, Integer, String, TypeDecorator, func
from sqlalchemy import DateTime as SqlDateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column

from massingbill.extensions import Base

__all__ = [
    "Base",
    "DateTime",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UtcDateTime",
    "UuidPrimaryKeyMixin",
    "bp_column",
    "money_column",
    "new_uuid",
    "utcnow",
]


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC, in and out.

    SQLite has no timezone type and hands back a naive ``datetime``, so
    comparing a stored value against ``utcnow()`` raises ``TypeError`` -- a
    failure that appears only on the SQLite path, which is exactly where
    development and the test suite live, and which would then behave
    differently again on Postgres.

    Normalising in one type decorator fixes the whole class of bug: every
    timestamp is stored as UTC and read back as aware UTC, on every backend.
    """

    impl = SqlDateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive value written by application code is meant as UTC.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


#: Use this everywhere instead of ``DateTime``. Named ``DateTime`` so a model
#: reads normally and nobody has to remember the distinction.
DateTime = UtcDateTime


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
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    """For non-financial rows only. Financial records use an explicit void state."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
