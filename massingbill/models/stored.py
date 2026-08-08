"""Stored materials.

G703 column F is the value of material bought and stored but **not yet
incorporated into the work**. When it is installed it moves into column E --
and that move is the single most dangerous operation in a pay application,
because doing it as two independent edits bills the same material twice.

So the roll is modelled explicitly: a stored material knows which application
installed it, and the period engine reads that rather than trusting anyone to
remember to reduce F by hand.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin, money_column


class StorageLocation(StrEnum):
    """Where the material is, which decides what backup a contract demands.

    Off-site storage is the contentious one: most owners will not pay for it
    without a bond, proof of insurance and evidence of title.
    """

    ONSITE = "onsite"
    OFFSITE = "offsite"
    BONDED_OFFSITE = "bonded_offsite"


class StoredMaterial(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stored_materials"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sov_line_id: Mapped[str] = mapped_column(
        ForeignKey("sov_lines.id", ondelete="CASCADE"), nullable=False, index=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[StorageLocation] = mapped_column(
        String(32), nullable=False, default=StorageLocation.ONSITE
    )
    value_cents: Mapped[int] = money_column()

    #: The period this material first appeared in column F.
    first_billed_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )
    #: The period it was installed and rolled from F into E. While this is
    #: null the value stays in column F.
    installed_in_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )

    # Backup an owner will ask for before paying for material they cannot see.
    supplier: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    invoice_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    bond_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    insurance_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    stored_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_void: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sov_line: Mapped[object] = relationship("SovLine", viewonly=True)

    @property
    def is_installed(self) -> bool:
        return self.installed_in_application_id is not None

    @property
    def has_backup(self) -> bool:
        return bool(self.invoice_ref)

    @property
    def is_offsite(self) -> bool:
        return self.location in (StorageLocation.OFFSITE, StorageLocation.BONDED_OFFSITE)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StoredMaterial {self.description[:24]} {self.value_cents}>"
