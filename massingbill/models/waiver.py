"""Lien waivers.

A waiver releases lien rights, which is the most consequential document this
system produces. Three things follow from that and are enforced in the schema:

**Templates are effective-dated.** A waiver renders the text that was in force
on the period it releases, not the text in force today.

**Statutory templates carry a verification flag.** Twelve states prescribe the
wording, and a form that does not substantially conform can be unenforceable.
An unverified statutory template refuses to render (``services/waivers.py``).

**The signature binds the rendered bytes.** ``document_sha256`` is the digest of
the exact document that was signed, so re-rendering it after an edit
invalidates the signature rather than silently re-attaching it to different
words.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from massingbill.models.base import (
    Base,
    DateTime,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    money_column,
)

if TYPE_CHECKING:
    from massingbill.models.application import Application


class WaiverType(StrEnum):
    """The four waivers every jurisdiction recognises.

    The conditional/unconditional distinction is the one that matters: a
    conditional waiver takes effect only on the claimant actually being paid,
    an unconditional one takes effect on signature. Signing an unconditional
    waiver before the cheque clears is how contractors lose lien rights.
    """

    CONDITIONAL_PROGRESS = "conditional_progress"
    UNCONDITIONAL_PROGRESS = "unconditional_progress"
    CONDITIONAL_FINAL = "conditional_final"
    UNCONDITIONAL_FINAL = "unconditional_final"

    @property
    def is_conditional(self) -> bool:
        return self in (WaiverType.CONDITIONAL_PROGRESS, WaiverType.CONDITIONAL_FINAL)

    @property
    def is_final(self) -> bool:
        return self in (WaiverType.CONDITIONAL_FINAL, WaiverType.UNCONDITIONAL_FINAL)

    @property
    def label(self) -> str:
        return WAIVER_TYPE_LABELS[self]


WAIVER_TYPE_LABELS = {
    WaiverType.CONDITIONAL_PROGRESS: "Conditional waiver on progress payment",
    WaiverType.UNCONDITIONAL_PROGRESS: "Unconditional waiver on progress payment",
    WaiverType.CONDITIONAL_FINAL: "Conditional waiver on final payment",
    WaiverType.UNCONDITIONAL_FINAL: "Unconditional waiver on final payment",
}


class WaiverStatus(StrEnum):
    DRAFT = "draft"
    REQUESTED = "requested"
    SIGNED = "signed"
    NOTARIZED = "notarized"
    REJECTED = "rejected"
    VOID = "void"


class WaiverTemplate(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One waiver form, for one jurisdiction, effective over a date range."""

    __tablename__ = "waiver_templates"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "state",
            "waiver_type",
            "effective_from",
            name="uq_waiver_template",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Two-letter code, or empty for the general form used wherever no wording
    #: is prescribed.
    state: Mapped[str] = mapped_column(String(2), nullable=False, default="", index=True)
    waiver_type: Mapped[WaiverType] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    required_fields: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    is_statutory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    must_match_exactly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notary_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    residential_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: A statutory template is unusable until someone has read the statute and
    #: entered the verbatim text. Rendering refuses while this is false.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    citation: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    @property
    def is_usable(self) -> bool:
        """A statutory form with no verified text must never be issued."""
        return bool(self.body.strip()) and (self.verified or not self.is_statutory)

    def covers(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WaiverTemplate {self.state or 'general'} {self.waiver_type}>"


class WaiverInstance(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A waiver requested against one application."""

    __tablename__ = "waiver_instances"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("waiver_templates.id", ondelete="RESTRICT"), nullable=False
    )
    #: Set when the waiver is from a subcontractor rather than the GC.
    subcontract_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    waiver_type: Mapped[WaiverType] = mapped_column(String(32), nullable=False)
    status: Mapped[WaiverStatus] = mapped_column(
        String(32), nullable=False, default=WaiverStatus.DRAFT
    )

    claimant: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    customer: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    #: The amount the waiver releases. Checked against the payment it covers --
    #: a waiver for the wrong amount is how rights are released for work that
    #: has not been paid for.
    amount_cents: Mapped[int] = money_column()
    through_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: The rendered document, frozen at request time so the signature binds
    #: words nobody can change afterwards.
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rendered_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notarized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    application: Mapped[Application] = relationship()
    template: Mapped[WaiverTemplate] = relationship()
    signature: Mapped[Signature | None] = relationship(
        back_populates="waiver", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_signed(self) -> bool:
        return self.status in (WaiverStatus.SIGNED, WaiverStatus.NOTARIZED)

    @property
    def is_conditional(self) -> bool:
        return WaiverType(self.waiver_type).is_conditional

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WaiverInstance {self.waiver_type} {self.amount_cents} ({self.status})>"


class Signature(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """An ESIGN/UETA evidence record.

    What makes an electronic signature hold up is not the image of a name --
    it is the record showing *who* signed, *what* they signed, *when*, and that
    they consented to sign electronically. So all of that is captured, and
    ``document_sha256`` pins the exact bytes.
    """

    __tablename__ = "signatures"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    waiver_id: Mapped[str | None] = mapped_column(
        ForeignKey("waiver_instances.id", ondelete="CASCADE"), nullable=True, unique=True
    )

    signer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    signer_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    signer_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    signer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: The digest of exactly what was signed. Re-rendering after an edit
    #: produces a different digest, which invalidates the signature rather than
    #: silently re-attaching it to different words.
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: ESIGN/UETA requires affirmative consent to transact electronically, and
    #: requires it to be recorded.
    consent_text: Mapped[str] = mapped_column(Text, nullable=False)
    consented: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    #: An externally-executed document (DocuSign, wet ink) attaches here rather
    #: than being re-keyed, so the evidence stays with the signature.
    external_reference: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    waiver: Mapped[WaiverInstance | None] = relationship(back_populates="signature")

    def matches(self, document_sha256: str) -> bool:
        return self.document_sha256 == document_sha256
