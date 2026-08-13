"""Spent handoff assertions.

A signed assertion from the massing.cloud bridge is good for one sign-in. The
signature alone cannot enforce that -- anyone holding a captured URL can present
it again -- so the ``jti`` is recorded here the first time it is used and
refused thereafter.

The table is deliberately trivial: a primary key and a timestamp. The primary
key *is* the protection. Two simultaneous requests presenting the same
assertion both try to insert the same row, and the database decides which one
wins; a check-then-insert in application code would let both through.

Rows are worthless once the assertion they describe could no longer be valid,
and :func:`massingbill.services.handoff.prune` deletes them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from massingbill.models.base import Base, UtcDateTime, utcnow


class SpentHandoff(Base):
    """One assertion that has already been used."""

    __tablename__ = "spent_handoffs"

    #: The bridge's ``jti``, which is its uuid4. Not a surrogate key: the whole
    #: point is that presenting the same one twice collides.
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)

    used_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SpentHandoff {self.jti}>"
