"""Compliance documents: what is on file, what has lapsed, what blocks payment.

The distinction that matters is between *warning* and *refusing*. A GC who has
agreed with an owner to hold funds until certified payroll is in hand needs the
system to refuse, or the agreement is decorative. A GC who merely likes to have
a W-9 on file wants a nudge. ``blocks_payment`` decides which.

Currency is judged **as at the period end**, not today. A certificate that has
lapsed since does not retroactively make last quarter's work uninsured, and
failing an old application for it would be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from massingbill.extensions import db
from massingbill.models import (
    Application,
    ComplianceDoc,
    ComplianceKind,
    ComplianceRequirement,
    Project,
    User,
)
from massingbill.services import audit


@dataclass(frozen=True)
class ComplianceState:
    """Where one requirement stands for one period."""

    requirement: ComplianceRequirement
    document: ComplianceDoc | None
    satisfied: bool
    reason: str
    expires_in_days: int | None = None

    @property
    def blocks(self) -> bool:
        return not self.satisfied and self.requirement.blocks_payment

    @property
    def expiring_soon(self) -> bool:
        if self.expires_in_days is None or not self.satisfied:
            return False
        return self.expires_in_days <= self.requirement.warn_days_before_expiry


def requirements_for(
    project: Project, *, subcontract_id: str | None = None
) -> list[ComplianceRequirement]:
    return list(
        db.session.scalars(
            select(ComplianceRequirement)
            .where(
                ComplianceRequirement.project_id == project.id,
                ComplianceRequirement.subcontract_id.is_(subcontract_id)
                if subcontract_id is None
                else ComplianceRequirement.subcontract_id == subcontract_id,
            )
            .order_by(ComplianceRequirement.kind)
        )
    )


def add_requirement(
    project: Project,
    kind: ComplianceKind,
    *,
    blocks_payment: bool = True,
    is_recurring: bool = False,
    description: str = "",
    subcontract_id: str | None = None,
    actor: User | None = None,
) -> ComplianceRequirement:
    requirement = ComplianceRequirement(
        organization_id=project.organization_id,
        project_id=project.id,
        subcontract_id=subcontract_id,
        kind=kind,
        description=description,
        blocks_payment=blocks_payment,
        is_recurring=is_recurring,
    )
    db.session.add(requirement)
    db.session.flush()

    audit.record(
        project.organization_id,
        audit.COMPLIANCE_REQUIRED,
        entity_type="compliance_requirement",
        entity_id=requirement.id,
        after={"kind": str(kind), "blocks_payment": blocks_payment},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return requirement


def file_document(
    requirement: ComplianceRequirement,
    *,
    filename: str,
    storage_key: str = "",
    sha256: str = "",
    effective_from: date | None = None,
    expires_on: date | None = None,
    application: Application | None = None,
    actor: User | None = None,
) -> ComplianceDoc:
    document = ComplianceDoc(
        organization_id=requirement.organization_id,
        requirement_id=requirement.id,
        application_id=application.id if application is not None else None,
        filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        effective_from=effective_from,
        expires_on=expires_on,
    )
    db.session.add(document)
    db.session.flush()

    audit.record(
        requirement.organization_id,
        audit.COMPLIANCE_FILED,
        entity_type="compliance_doc",
        entity_id=document.id,
        after={"kind": str(requirement.kind), "expires_on": str(expires_on or "")},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return document


def evaluate(
    application: Application, *, subcontract_id: str | None = None
) -> list[ComplianceState]:
    """Where every requirement stands for this application's period."""
    project = application.prime_contract.project
    on = application.period_end
    states: list[ComplianceState] = []

    for requirement in requirements_for(project, subcontract_id=subcontract_id):
        documents = [
            doc
            for doc in requirement.documents
            if not doc.is_void
            and (
                # A recurring requirement is satisfied only by a document filed
                # for *this* period: last month's certified payroll says nothing
                # about this month's.
                not requirement.is_recurring or doc.application_id == application.id
            )
        ]
        current = [doc for doc in documents if doc.is_current(on)]

        if current:
            best = min(current, key=lambda d: (d.expires_on is None, d.expires_on or on))
            states.append(
                ComplianceState(
                    requirement=requirement,
                    document=best,
                    satisfied=True,
                    reason="",
                    expires_in_days=best.days_until_expiry(on),
                )
            )
        elif documents:
            lapsed = max(documents, key=lambda d: d.expires_on or on)
            states.append(
                ComplianceState(
                    requirement=requirement,
                    document=lapsed,
                    satisfied=False,
                    reason=(
                        f"expired {lapsed.expires_on.isoformat()}"
                        if lapsed.expires_on
                        else "not valid for this period"
                    ),
                )
            )
        else:
            states.append(
                ComplianceState(
                    requirement=requirement,
                    document=None,
                    satisfied=False,
                    reason="nothing on file"
                    if not requirement.is_recurring
                    else "nothing on file for this period",
                )
            )

    return states


def blocking(application: Application) -> list[ComplianceState]:
    return [state for state in evaluate(application) if state.blocks]


def expiring(application: Application) -> list[ComplianceState]:
    return [state for state in evaluate(application) if state.expiring_soon]
