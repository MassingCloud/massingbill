"""Projects, prime contracts and the organization's member list."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from massingbill.errors import ConflictError
from massingbill.extensions import db
from massingbill.models import (
    Membership,
    PrimeContract,
    Project,
    RetainageMode,
    RetainageRule,
    Role,
    User,
)
from massingbill.services import accounts, audit
from massingbill.services import sov as sov_service
from massingbill.services.rbac import (
    AUDIT_READ,
    ORG_MANAGE,
    PROJECT_CREATE,
    PROJECT_READ,
    PROJECT_UPDATE,
    active_membership,
    active_organization,
    active_organization_id,
    get_scoped_or_404,
    has_permission,
    require_organization_id,
    require_permission,
    scoped,
)

bp = Blueprint("projects", __name__)


@bp.get("/projects")
@login_required
@require_permission(PROJECT_READ)
def index() -> Any:
    projects = list(db.session.scalars(scoped(Project).order_by(Project.number)))
    return render_template(
        "projects/index.html",
        projects=projects,
        organization=active_organization(),
        memberships=accounts.memberships_for(current_user),
        can_create=has_permission(PROJECT_CREATE),
    )


@bp.get("/projects/new")
@login_required
@require_permission(PROJECT_CREATE)
def new() -> Any:
    from massingbill.blueprints.forms import ProjectForm

    return render_template("projects/edit.html", form=ProjectForm(), project=None)


@bp.post("/projects/new")
@login_required
@require_permission(PROJECT_CREATE)
def create() -> Any:
    from massingbill.blueprints.forms import ProjectForm

    form = ProjectForm()
    if not form.validate_on_submit():
        return render_template("projects/edit.html", form=form, project=None), 400

    organization_id = require_organization_id()

    duplicate = db.session.scalar(scoped(Project).where(Project.number == form.number.data))
    if duplicate is not None:
        raise ConflictError(f"Project number {form.number.data!r} is already in use.")

    project = Project(
        organization_id=organization_id,
        number=(form.number.data or "").strip(),
        name=(form.name.data or "").strip(),
        address=(form.address.data or "").strip(),
        jurisdiction_state=(form.jurisdiction_state.data or "").upper(),
        is_public_work=bool(form.is_public_work.data),
        is_residential=bool(form.is_residential.data),
        stories=form.stories.data,
    )
    db.session.add(project)
    db.session.flush()

    audit.record_for_current_user(
        organization_id,
        audit.PROJECT_CREATED,
        entity_type="project",
        entity_id=project.id,
        after={"number": project.number, "name": project.name},
    )
    db.session.commit()

    flash(f"Project {project.number} created.", "success")
    return redirect(url_for("projects.detail", project_id=project.id))


@bp.get("/projects/<project_id>")
@login_required
@require_permission(PROJECT_READ)
def detail(project_id: str) -> Any:
    project = get_scoped_or_404(Project, project_id)
    contract = project.prime_contract
    schedule = sov_service.current_schedule(contract) if contract else None

    return render_template(
        "projects/detail.html",
        project=project,
        contract=contract,
        schedule=schedule,
        reconciliation=sov_service.reconciliation(schedule) if schedule else None,
        can_edit=has_permission(PROJECT_UPDATE),
    )


@bp.get("/projects/<project_id>/edit")
@login_required
@require_permission(PROJECT_UPDATE)
def edit(project_id: str) -> Any:
    from massingbill.blueprints.forms import ProjectForm

    project = get_scoped_or_404(Project, project_id)
    return render_template("projects/edit.html", form=ProjectForm(obj=project), project=project)


@bp.post("/projects/<project_id>/edit")
@login_required
@require_permission(PROJECT_UPDATE)
def update(project_id: str) -> Any:
    from massingbill.blueprints.forms import ProjectForm

    project = get_scoped_or_404(Project, project_id)
    form = ProjectForm()
    if not form.validate_on_submit():
        return render_template("projects/edit.html", form=form, project=project), 400

    before = {"number": project.number, "name": project.name, "state": project.jurisdiction_state}

    project.number = (form.number.data or "").strip()
    project.name = (form.name.data or "").strip()
    project.address = (form.address.data or "").strip()
    project.jurisdiction_state = (form.jurisdiction_state.data or "").upper()
    project.is_public_work = bool(form.is_public_work.data)
    project.is_residential = bool(form.is_residential.data)
    project.stories = form.stories.data

    audit.record_for_current_user(
        project.organization_id,
        audit.PROJECT_UPDATED,
        entity_type="project",
        entity_id=project.id,
        before=before,
        after={"number": project.number, "name": project.name, "state": project.jurisdiction_state},
    )
    db.session.commit()

    flash("Project saved.", "success")
    return redirect(url_for("projects.detail", project_id=project.id))


# ── Prime contract ──────────────────────────────────────────────────────────


@bp.get("/projects/<project_id>/contract")
@login_required
@require_permission(PROJECT_UPDATE)
def contract_form(project_id: str) -> Any:
    from massingbill.blueprints.forms import ContractForm

    project = get_scoped_or_404(Project, project_id)
    contract = project.prime_contract

    form = ContractForm()
    if contract is not None:
        form.number.data = contract.number
        form.original_contract_sum.data = contract.original_contract_sum_cents
        form.stored_materials_allowed.data = contract.stored_materials_allowed
        form.offsite_stored_allowed.data = contract.offsite_stored_allowed
        if contract.retainage_rule is not None:
            form.retainage_rate_work.data = contract.retainage_rule.rate_work_bp
            form.retainage_rate_stored.data = contract.retainage_rule.rate_stored_bp
    else:
        form.retainage_rate_work.data = 1000
        form.retainage_rate_stored.data = 1000

    return render_template("projects/contract.html", form=form, project=project, contract=contract)


@bp.post("/projects/<project_id>/contract")
@login_required
@require_permission(PROJECT_UPDATE)
def save_contract(project_id: str) -> Any:
    from massingbill.blueprints.forms import ContractForm

    project = get_scoped_or_404(Project, project_id)
    contract = project.prime_contract
    form = ContractForm()

    if not form.validate_on_submit():
        return (
            render_template(
                "projects/contract.html", form=form, project=project, contract=contract
            ),
            400,
        )

    creating = contract is None
    if contract is None:
        rule = RetainageRule(
            organization_id=project.organization_id,
            mode=RetainageMode.SPLIT,
            rate_work_bp=form.retainage_rate_work.data or 0,
            rate_stored_bp=form.retainage_rate_stored.data or 0,
        )
        db.session.add(rule)
        db.session.flush()

        contract = PrimeContract(
            organization_id=project.organization_id,
            project_id=project.id,
            retainage_rule_id=rule.id,
        )
        db.session.add(contract)

    before = None if creating else {"sum_cents": contract.original_contract_sum_cents}

    contract.number = (form.number.data or "").strip()
    contract.original_contract_sum_cents = int(form.original_contract_sum.data or 0)
    contract.stored_materials_allowed = bool(form.stored_materials_allowed.data)
    contract.offsite_stored_allowed = bool(form.offsite_stored_allowed.data)

    if contract.retainage_rule is not None:
        contract.retainage_rule.rate_work_bp = form.retainage_rate_work.data or 0
        contract.retainage_rule.rate_stored_bp = form.retainage_rate_stored.data or 0

    db.session.flush()

    audit.record_for_current_user(
        project.organization_id,
        audit.CONTRACT_CREATED if creating else audit.CONTRACT_UPDATED,
        entity_type="prime_contract",
        entity_id=contract.id,
        before=before,
        after={"sum_cents": contract.original_contract_sum_cents},
    )
    db.session.commit()

    flash("Contract saved.", "success")
    return redirect(url_for("projects.detail", project_id=project.id))


# ── Organization members ────────────────────────────────────────────────────


@bp.get("/organization/members")
@login_required
@require_permission(ORG_MANAGE)
def members() -> Any:
    from massingbill.blueprints.forms import InviteMemberForm

    organization_id = active_organization_id()
    memberships = list(
        db.session.scalars(
            select(Membership)
            .where(Membership.organization_id == organization_id)
            .order_by(Membership.created_at)
        )
    )
    return render_template(
        "organization/members.html",
        memberships=memberships,
        form=InviteMemberForm(),
        organization=active_organization(),
        roles=list(Role),
    )


@bp.post("/organization/members")
@login_required
@require_permission(ORG_MANAGE)
def add_member() -> Any:
    from massingbill.blueprints.forms import InviteMemberForm

    form = InviteMemberForm()
    organization = active_organization()
    if organization is None:
        abort(401)

    if not form.validate_on_submit():
        flash("Check the details and try again.", "error")
        return redirect(url_for("projects.members"))

    email = accounts.normalize_email(form.email.data or "")
    user = accounts.get_user_by_email(email)
    if user is None:
        # P2 adds existing users directly; emailed invitations land with the
        # notification work in P6.
        flash(
            f"No account exists for {email}. They need to register first.",
            "error",
        )
        return redirect(url_for("projects.members"))

    accounts.add_member(organization, user, Role(form.role.data), actor=current_user)
    db.session.commit()

    flash(f"{email} added.", "success")
    return redirect(url_for("projects.members"))


@bp.post("/organization/members/<membership_id>/role")
@login_required
@require_permission(ORG_MANAGE)
def change_role(membership_id: str) -> Any:
    from massingbill.blueprints.forms import ChangeRoleForm

    membership = get_scoped_or_404(Membership, membership_id)
    form = ChangeRoleForm()
    if not form.validate_on_submit():
        return redirect(url_for("projects.members"))

    accounts.change_member_role(membership, Role(form.role.data), actor=current_user)
    db.session.commit()

    flash("Role updated.", "success")
    return redirect(url_for("projects.members"))


@bp.post("/organization/members/<membership_id>/remove")
@login_required
@require_permission(ORG_MANAGE)
def remove_member(membership_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    membership = get_scoped_or_404(Membership, membership_id)
    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("projects.members"))

    accounts.remove_member(membership, actor=current_user)
    db.session.commit()

    flash("Member removed.", "success")
    return redirect(url_for("projects.members"))


# ── Audit ───────────────────────────────────────────────────────────────────


@bp.get("/organization/audit")
@login_required
@require_permission(AUDIT_READ)
def audit_log() -> Any:
    from massingbill.models import AuditEvent

    organization_id = require_organization_id()

    events = list(
        db.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(200)
        )
    )
    return render_template(
        "organization/audit.html",
        events=events,
        verdict=audit.verify(organization_id),
    )


__all__ = ["User", "active_membership", "bp"]
