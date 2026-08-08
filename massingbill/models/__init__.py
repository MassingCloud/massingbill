"""Model package.

Importing this module must import every mapped class, so Alembic's autogenerate
sees the full metadata. Domain tables land in P2 and P3; the base helpers are
here from P0 because the money-column decision must not be relitigated later.
"""

from __future__ import annotations

from .base import (
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    bp_column,
    money_column,
    new_uuid,
    utcnow,
)

__all__ = [
    "Base",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UuidPrimaryKeyMixin",
    "bp_column",
    "money_column",
    "new_uuid",
    "utcnow",
]
