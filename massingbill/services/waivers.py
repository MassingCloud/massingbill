"""Lien waivers: selecting the right form, rendering it, and checking the amount.

Three jobs, in the order they matter.

**Select the right form.** Effective-dated by the period being released, and
jurisdiction-specific. Twelve states prescribe the wording; the rest use the
general form.

**Refuse to render an unverified statutory form.** This is the load-bearing
decision of the module. A waiver that does not substantially conform to the
statute can be unenforceable, so the seed ships statutory bodies empty and this
function refuses them by name and citation. Being unable to issue a waiver is
recoverable in an afternoon. Issuing one built from invented statutory language
is not recoverable at all.

**Check the amount.** Handle's "waiver protection safeguards", expressed as
data the tie-out engine reads: a waiver states an amount, and that amount has
to match the payment it releases. A conditional waiver for the wrong figure
releases rights for work nobody has paid for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    Organization,
    Project,
    Signature,
    User,
    WaiverInstance,
    WaiverStatus,
    WaiverTemplate,
    WaiverType,
)
from massingbill.models.base import utcnow
from massingbill.services import audit
from massingbill.services.money import Cents, cents, to_display

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed" / "waivers"

#: The consent an electronic signature needs on the record to satisfy
#: ESIGN/UETA. Stored with each signature rather than referenced, so the
#: evidence is complete even if this string later changes.
CONSENT_TEXT = (
    "I agree to sign this document electronically. I understand that my "
    "electronic signature has the same legal effect as a handwritten one, that "
    "this document waives lien and payment-bond rights to the extent stated, "
    "and that I may request a paper copy instead."
)

#: Sandboxed, because a statutory body is *entered by an administrator* and
#: then rendered as a template. Without the sandbox, `{{ config }}` in a waiver
#: body would read the application's secrets -- server-side template injection
#: by whoever transcribes a statute.
#:
#: StrictUndefined so a form referencing a field nobody supplied fails loudly
#: rather than producing a waiver with a blank where an amount should be.
#:
#: autoescape stays off on purpose: the output is plain text, and escaping would
#: turn an ampersand in a company name into "&amp;" on the face of a legal
#: document. The HTML boundary is where escaping belongs, and Jinja autoescapes
#: `{{ waiver.rendered_body }}` in the document templates by default.
_JINJA = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


# ── Seeding ─────────────────────────────────────────────────────────────────


def seed_templates(organization: Organization) -> int:
    """Load the waiver forms. Idempotent; returns how many were added."""
    added = _seed_general(organization)
    added += _seed_statutory(organization)
    db.session.flush()
    return added


def _existing_keys(organization: Organization) -> set[tuple[str, str, date]]:
    rows = db.session.execute(
        select(
            WaiverTemplate.state, WaiverTemplate.waiver_type, WaiverTemplate.effective_from
        ).where(WaiverTemplate.organization_id == organization.id)
    ).all()
    return {(state, str(waiver_type), effective) for state, waiver_type, effective in rows}


def _seed_general(organization: Organization) -> int:
    data = yaml.safe_load((SEED_DIR / "general.yaml").read_text(encoding="utf-8"))
    existing = _existing_keys(organization)
    effective = date.fromisoformat(str(data["effective_from"]))

    added = 0
    for type_name, form in data["types"].items():
        if ("", type_name, effective) in existing:
            continue
        db.session.add(
            WaiverTemplate(
                organization_id=organization.id,
                state="",
                waiver_type=WaiverType(type_name),
                title=form["title"],
                body=form["body"],
                required_fields=",".join(form.get("required_fields", [])),
                is_statutory=False,
                must_match_exactly=False,
                notary_required=bool(form.get("notary_required", False)),
                verified=True,
                effective_from=effective,
            )
        )
        added += 1
    return added


def _seed_statutory(organization: Organization) -> int:
    data = yaml.safe_load((SEED_DIR / "statutory.yaml").read_text(encoding="utf-8"))
    defaults: dict[str, Any] = data["defaults"]
    existing = _existing_keys(organization)

    added = 0
    for entry in data["states"]:
        effective = date.fromisoformat(str(entry.get("effective_from", defaults["effective_from"])))
        for type_name in entry.get("types", defaults["types"]):
            if (entry["state"], type_name, effective) in existing:
                continue
            db.session.add(
                WaiverTemplate(
                    organization_id=organization.id,
                    state=entry["state"],
                    waiver_type=WaiverType(type_name),
                    title=f"{entry['state']} statutory {WaiverType(type_name).label.lower()}",
                    # Deliberately empty. See data/seed/waivers/README.md.
                    body="",
                    required_fields=",".join(
                        entry.get("required_fields", defaults["required_fields"])
                    ),
                    is_statutory=True,
                    must_match_exactly=bool(entry.get("must_match_exactly", True)),
                    notary_required=bool(entry.get("notary_required", defaults["notary_required"])),
                    residential_only=bool(entry.get("residential_only", False)),
                    verified=False,
                    citation=entry["citation"],
                    note=entry.get("note", ""),
                    effective_from=effective,
                )
            )
            added += 1
    return added


# ── Selecting a form ────────────────────────────────────────────────────────


def template_for(
    organization_id: str,
    *,
    state: str,
    waiver_type: WaiverType,
    on: date,
    is_residential: bool = False,
) -> WaiverTemplate:
    """The form in force for this jurisdiction on this date.

    Falls back to the general form where no wording is prescribed -- and, for
    Missouri, where the statutory form applies to residential work only.
    """
    candidates = list(
        db.session.scalars(
            select(WaiverTemplate).where(
                WaiverTemplate.organization_id == organization_id,
                WaiverTemplate.waiver_type == waiver_type,
                WaiverTemplate.state.in_((state.upper(), "")),
            )
        )
    )
    in_force = [template for template in candidates if template.covers(on)]

    statutory = [
        template
        for template in in_force
        if template.state == state.upper() and (not template.residential_only or is_residential)
    ]
    if statutory:
        # Newest effective date wins when a statute has been amended.
        return max(statutory, key=lambda t: t.effective_from)

    general = [template for template in in_force if template.state == ""]
    if general:
        return max(general, key=lambda t: t.effective_from)

    raise NotFoundError(
        f"No {waiver_type.label.lower()} form is on file for {state or 'this jurisdiction'} "
        f"as at {on.isoformat()}."
    )


# ── Rendering ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WaiverFields:
    claimant: str
    customer: str
    project: str
    amount: Cents
    through_date: date
    disputed_amount: Cents = field(default_factory=lambda: cents(0))

    def as_context(self) -> dict[str, str]:
        return {
            "claimant": self.claimant,
            "customer": self.customer,
            "project": self.project,
            "amount": to_display(self.amount),
            "through_date": self.through_date.isoformat(),
            "disputed_amount": to_display(self.disputed_amount),
        }


def render_body(template: WaiverTemplate, fields: WaiverFields) -> str:
    """Fill the form.

    Refuses an unverified statutory template. This is the point of the whole
    module: twelve states prescribe the wording, a form that does not
    substantially conform can be unenforceable, and a waiver built from invented
    statutory language releases rights nobody would discover were gone until the
    money was.
    """
    if not template.is_usable:
        raise ConflictError(
            f"The {template.state} statutory waiver form has not been verified, so it "
            f"cannot be issued. Enter the verbatim text from {template.citation} and mark "
            "the template verified. Massing Bill ships statutory forms empty on purpose: "
            "a waiver that does not conform to the statute can be unenforceable."
        )

    missing = [
        name
        for name in filter(None, template.required_fields.split(","))
        if not str(fields.as_context().get(name, "")).strip()
    ]
    if missing:
        raise ValidationError(
            f"This waiver form requires {', '.join(missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} missing."
        )

    return _JINJA.from_string(template.body).render(**fields.as_context()).strip()


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ── The waiver lifecycle ────────────────────────────────────────────────────


def request(
    application: Application,
    *,
    waiver_type: WaiverType,
    claimant: str,
    customer: str,
    amount: Cents,
    through_date: date | None = None,
    actor: User | None = None,
) -> WaiverInstance:
    """Create and render a waiver against one application."""
    project = application.prime_contract.project
    releases_on = through_date or application.period_end

    template = template_for(
        application.organization_id,
        state=project.jurisdiction_state,
        waiver_type=waiver_type,
        on=releases_on,
        is_residential=project.is_residential,
    )

    body = render_body(
        template,
        WaiverFields(
            claimant=claimant,
            customer=customer,
            project=f"{project.number} — {project.name}",
            amount=amount,
            through_date=releases_on,
        ),
    )

    waiver = WaiverInstance(
        organization_id=application.organization_id,
        application_id=application.id,
        template_id=template.id,
        waiver_type=waiver_type,
        status=WaiverStatus.REQUESTED,
        claimant=claimant,
        customer=customer,
        amount_cents=int(amount),
        through_date=releases_on,
        rendered_body=body,
        rendered_sha256=digest(body),
        requested_at=utcnow(),
    )
    db.session.add(waiver)
    db.session.flush()

    audit.record(
        application.organization_id,
        audit.WAIVER_REQUESTED,
        entity_type="waiver",
        entity_id=waiver.id,
        after={"type": str(waiver_type), "amount_cents": waiver.amount_cents},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return waiver


def sign(
    waiver: WaiverInstance,
    *,
    signer_name: str,
    signer_title: str = "",
    signer_email: str = "",
    consented: bool,
    ip: str = "",
    user_agent: str = "",
    signer: User | None = None,
    external_reference: str = "",
) -> Signature:
    """Record a signature against the exact rendered document.

    ESIGN/UETA turns on consent and on being able to show what was signed, so
    both are required: an unconsented signature is refused, and the digest of
    the rendered body is stored with it.
    """
    if waiver.status not in (WaiverStatus.DRAFT, WaiverStatus.REQUESTED):
        raise ConflictError(f"This waiver is {waiver.status} and cannot be signed again.")

    if not consented:
        raise ValidationError(
            "An electronic signature needs recorded consent to sign electronically."
        )
    if not signer_name.strip():
        raise ValidationError("A signature needs the signer's name.")

    signature = Signature(
        organization_id=waiver.organization_id,
        waiver_id=waiver.id,
        signer_name=signer_name.strip(),
        signer_title=signer_title.strip(),
        signer_email=signer_email.strip().lower(),
        signer_user_id=signer.id if signer else None,
        document_sha256=waiver.rendered_sha256,
        consent_text=CONSENT_TEXT,
        consented=True,
        signed_at=utcnow(),
        ip=ip,
        user_agent=user_agent[:500],
        external_reference=external_reference,
    )
    db.session.add(signature)

    waiver.status = WaiverStatus.SIGNED
    waiver.signed_at = signature.signed_at
    db.session.flush()

    audit.record(
        waiver.organization_id,
        audit.WAIVER_SIGNED,
        entity_type="waiver",
        entity_id=waiver.id,
        after={"signer": signature.signer_name, "sha256": signature.document_sha256},
        actor_id=signer.id if signer else None,
        actor_label=signer.email if signer else signature.signer_email,
    )
    return signature


def notarize(waiver: WaiverInstance, *, notary_reference: str) -> None:
    if not waiver.is_signed:
        raise ConflictError("A waiver must be signed before it can be notarised.")
    waiver.status = WaiverStatus.NOTARIZED
    waiver.notarized_at = utcnow()
    if waiver.signature is not None:
        waiver.signature.external_reference = notary_reference
    db.session.flush()


def signature_is_intact(waiver: WaiverInstance) -> bool:
    """True when the signature still binds the document as it stands.

    Re-rendering a waiver after an edit changes the digest, which detaches the
    signature rather than silently carrying it over to different words.
    """
    if waiver.signature is None:
        return False
    return waiver.signature.matches(digest(waiver.rendered_body))


def for_application(application: Application) -> list[WaiverInstance]:
    return list(
        db.session.scalars(
            select(WaiverInstance)
            .where(
                WaiverInstance.application_id == application.id,
                WaiverInstance.status != WaiverStatus.VOID,
            )
            .order_by(WaiverInstance.created_at)
        )
    )


def unverified_templates(organization_id: str) -> list[WaiverTemplate]:
    """Statutory forms that cannot be issued yet -- for an admin screen."""
    return list(
        db.session.scalars(
            select(WaiverTemplate)
            .where(
                WaiverTemplate.organization_id == organization_id,
                WaiverTemplate.is_statutory.is_(True),
                WaiverTemplate.verified.is_(False),
            )
            .order_by(WaiverTemplate.state, WaiverTemplate.waiver_type)
        )
    )


def verify_template(template: WaiverTemplate, *, body: str, actor: User | None = None) -> None:
    """Record verbatim statutory text and mark the form usable."""
    if not body.strip():
        raise ValidationError("A verified statutory form needs its text.")

    template.body = body
    template.verified = True
    db.session.flush()

    audit.record(
        template.organization_id,
        audit.WAIVER_TEMPLATE_VERIFIED,
        entity_type="waiver_template",
        entity_id=template.id,
        after={"state": template.state, "type": str(template.waiver_type)},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


__all__ = [
    "CONSENT_TEXT",
    "Project",
    "WaiverFields",
    "digest",
    "for_application",
    "notarize",
    "render_body",
    "request",
    "seed_templates",
    "sign",
    "signature_is_intact",
    "template_for",
    "unverified_templates",
    "verify_template",
]
