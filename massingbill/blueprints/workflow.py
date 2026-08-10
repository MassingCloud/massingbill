"""Change orders, lien waivers, compliance documents and subcontracts.

Four features in one blueprint because they share a project and a shape: each is
a list, a create form, and a small number of state transitions. Splitting them
into four modules would quadruple the routing boilerplate without separating
anything that is actually separate.

The waiver screens carry the product's sharpest edge. A statutory form whose
verbatim text has not been entered **refuses to render**, by name and citation,
and the screen shows the refusal rather than falling back to something
plausible. That is the feature: a waiver that does not conform to the statute
can be unenforceable, and the person who discovers it is the one who has already
released their lien rights.
"""

from __future__ import annotations

from typing import Any, TypeVar

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from massingbill.errors import ConflictError, NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    ChangeOrder,
    ComplianceKind,
    PrimeContract,
    Project,
    Subcontract,
    WaiverInstance,
    WaiverTemplate,
    WaiverType,
)
from massingbill.services import change_order as co_service
from massingbill.services import compliance as compliance_service
from massingbill.services import sov as sov_service
from massingbill.services import subcontracts as sub_service
from massingbill.services import waivers as waiver_service
from massingbill.services.rbac import (
    CHANGE_ORDER_APPROVE,
    CHANGE_ORDER_WRITE,
    COMPLIANCE_READ,
    COMPLIANCE_WRITE,
    PROJECT_READ,
    SUBCONTRACT_READ,
    SUBCONTRACT_WRITE,
    WAIVER_READ,
    WAIVER_WRITE,
    active_organization,
    get_scoped_or_404,
    has_permission,
    require_permission,
)

bp = Blueprint("workflow", __name__, url_prefix="/projects/<project_id>")

T = TypeVar("T")


def _required(value: T | None, field: str) -> T:
    """Narrow a field validation has already required (see applications.py)."""
    if value is None:
        raise ValidationError(f"{field} is required.")
    return value


def _project(project_id: str) -> Project:
    return get_scoped_or_404(Project, project_id)


def _contract(project_id: str) -> PrimeContract:
    project = _project(project_id)
    if project.prime_contract is None:
        raise NotFoundError("This project has no prime contract yet.")
    return project.prime_contract


def _flash_errors(form: Any) -> None:
    for errors in form.errors.values():
        for error in errors:
            flash(error, "error")


# ── Change orders ───────────────────────────────────────────────────────────


@bp.get("/change-orders")
@login_required
@require_permission(PROJECT_READ)
def change_orders(project_id: str) -> Any:
    from massingbill.blueprints.forms import (
        ApproveChangeOrderForm,
        ChangeOrderForm,
        ChangeOrderLineForm,
    )

    contract = _contract(project_id)
    schedule = sov_service.approved_schedule(contract)

    line_form = ChangeOrderLineForm()
    line_form.sov_line_id.choices = [("", "— create a new line —")] + [
        (line.id, f"{line.item_no} — {line.description}")
        for line in (schedule.lines if schedule else [])
    ]

    return render_template(
        "workflow/change_orders.html",
        project=contract.project,
        contract=contract,
        orders=co_service.for_contract(contract),
        approved_total=co_service.approved_total(contract),
        form=ChangeOrderForm(),
        line_form=line_form,
        approve_form=ApproveChangeOrderForm(),
        can_write=has_permission(CHANGE_ORDER_WRITE),
        can_approve=has_permission(CHANGE_ORDER_APPROVE),
        has_approved_schedule=schedule is not None,
    )


@bp.post("/change-orders")
@login_required
@require_permission(CHANGE_ORDER_WRITE)
def create_change_order(project_id: str) -> Any:
    from massingbill.blueprints.forms import ChangeOrderForm

    contract = _contract(project_id)
    form = ChangeOrderForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.change_orders", project_id=project_id))

    co_service.create(
        contract,
        number=_required(form.number.data, "Number"),
        description=_required(form.description.data, "Description"),
        actor=current_user,
    )
    db.session.commit()
    flash("Change order created. Add its lines, then approve it.", "success")
    return redirect(url_for("workflow.change_orders", project_id=project_id))


@bp.post("/change-orders/<order_id>/lines")
@login_required
@require_permission(CHANGE_ORDER_WRITE)
def add_change_order_line(project_id: str, order_id: str) -> Any:
    from massingbill.blueprints.forms import ChangeOrderLineForm

    _contract(project_id)
    order = get_scoped_or_404(ChangeOrder, order_id)
    form = ChangeOrderLineForm()
    form.sov_line_id.choices = [(request.form.get("sov_line_id", ""), "")]

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.change_orders", project_id=project_id))

    sov_line_id = (form.sov_line_id.data or "").strip()
    new_item_no = (form.new_item_no.data or "").strip()

    # Either/or, checked here because only the view knows which lines exist.
    # "Both" would apply the amount twice; "neither" would apply it nowhere.
    if bool(sov_line_id) == bool(new_item_no):
        flash(
            "A change-order line either adjusts an existing schedule line or "
            "creates a new one. Choose exactly one.",
            "error",
        )
        return redirect(url_for("workflow.change_orders", project_id=project_id))

    from massingbill.models import SovLine

    co_service.add_line(
        order,
        amount=_required(form.amount.data, "Amount"),
        sov_line=get_scoped_or_404(SovLine, sov_line_id) if sov_line_id else None,
        new_item_no=new_item_no,
        description=form.description.data or "",
        csi_code=form.csi_code.data or "",
    )
    db.session.commit()
    flash("Line added.", "success")
    return redirect(url_for("workflow.change_orders", project_id=project_id))


@bp.post("/change-orders/<order_id>/approve")
@login_required
@require_permission(CHANGE_ORDER_APPROVE)
def approve_change_order(project_id: str, order_id: str) -> Any:
    from massingbill.blueprints.forms import ApproveChangeOrderForm

    contract = _contract(project_id)
    order = get_scoped_or_404(ChangeOrder, order_id)
    form = ApproveChangeOrderForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.change_orders", project_id=project_id))

    schedule = sov_service.approved_schedule(contract)
    if schedule is None:
        flash("Approve the schedule of values first.", "error")
        return redirect(url_for("workflow.change_orders", project_id=project_id))

    # A change order rewrites the schedule, so it lands on a *revision* rather
    # than on the approved one. Editing an approved schedule in place would
    # retroactively change what every issued application was built against.
    revision = sov_service.create_revision(schedule, actor=current_user)
    co_service.approve(
        order,
        revision,
        approved_date=_required(form.approved_date.data, "Approved on"),
        actor=current_user,
    )
    sov_service.approve(revision, actor=current_user)
    db.session.commit()

    flash(f"{order.number} approved. The schedule is now revision {revision.revision}.", "success")
    return redirect(url_for("workflow.change_orders", project_id=project_id))


# ── Lien waivers ────────────────────────────────────────────────────────────


@bp.get("/waivers")
@login_required
@require_permission(WAIVER_READ)
def waivers(project_id: str) -> Any:
    from massingbill.blueprints.forms import VerifyTemplateForm, WaiverRequestForm

    project = _project(project_id)
    contract = _contract(project_id)

    from massingbill.services import application as app_service

    applications = app_service.applications_for(contract)
    issued = [a for a in applications if a.is_issued]

    return render_template(
        "workflow/waivers.html",
        project=project,
        applications=issued,
        waivers=[w for a in applications for w in waiver_service.for_application(a)],
        unverified=waiver_service.unverified_templates(project.organization_id),
        form=WaiverRequestForm(),
        verify_form=VerifyTemplateForm(),
        can_write=has_permission(WAIVER_WRITE),
    )


@bp.post("/waivers")
@login_required
@require_permission(WAIVER_WRITE)
def request_waiver(project_id: str) -> Any:
    from massingbill.blueprints.forms import WaiverRequestForm

    project = _project(project_id)
    organization = active_organization()
    form = WaiverRequestForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.waivers", project_id=project_id))

    application = get_scoped_or_404(Application, request.form.get("application_id", ""))

    try:
        waiver_service.request(
            application,
            waiver_type=WaiverType(form.waiver_type.data),
            claimant=_required(form.claimant_name.data, "Claimant"),
            customer=organization.name if organization else project.number,
            amount=_required(form.amount.data, "Amount"),
            through_date=form.through_date.data,
            actor=current_user,
        )
    except ConflictError as exc:
        # The refusal *is* the feature. Shown in full, with its citation, rather
        # than reduced to "could not create waiver".
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("workflow.waivers", project_id=project_id))

    db.session.commit()
    flash("Waiver issued.", "success")
    return redirect(url_for("workflow.waivers", project_id=project_id))


@bp.get("/waivers/<waiver_id>")
@login_required
@require_permission(WAIVER_READ)
def show_waiver(project_id: str, waiver_id: str) -> Any:
    from massingbill.blueprints.forms import SignWaiverForm

    _project(project_id)
    waiver = get_scoped_or_404(WaiverInstance, waiver_id)

    return render_template(
        "workflow/waiver.html",
        project=_project(project_id),
        waiver=waiver,
        intact=waiver_service.signature_is_intact(waiver) if waiver.is_signed else None,
        form=SignWaiverForm(),
        can_write=has_permission(WAIVER_WRITE),
    )


@bp.post("/waivers/<waiver_id>/sign")
@login_required
@require_permission(WAIVER_WRITE)
def sign_waiver(project_id: str, waiver_id: str) -> Any:
    from massingbill.blueprints.forms import SignWaiverForm

    _project(project_id)
    waiver = get_scoped_or_404(WaiverInstance, waiver_id)
    form = SignWaiverForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.show_waiver", project_id=project_id, waiver_id=waiver_id))

    waiver_service.sign(
        waiver,
        signer_name=_required(form.signer_name.data, "Name"),
        signer_title=form.signer_title.data or "",
        signer_email=_required(form.signer_email.data, "Email"),
        consented=bool(form.intent.data),
        # ESIGN/UETA evidence: what was signed, by whom, from where, with what.
        ip=request.remote_addr or "",
        user_agent=request.user_agent.string[:500],
        signer=current_user,
    )
    db.session.commit()
    flash("Signed. The signature covers the exact document shown above.", "success")
    return redirect(url_for("workflow.show_waiver", project_id=project_id, waiver_id=waiver_id))


@bp.post("/waivers/templates/<template_id>/verify")
@login_required
@require_permission(WAIVER_WRITE)
def verify_template(project_id: str, template_id: str) -> Any:
    from massingbill.blueprints.forms import VerifyTemplateForm

    _project(project_id)
    template = get_scoped_or_404(WaiverTemplate, template_id)
    form = VerifyTemplateForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.waivers", project_id=project_id))

    waiver_service.verify_template(
        template, body=_required(form.body.data, "Body"), actor=current_user
    )
    db.session.commit()
    flash(
        f"{template.state} {template.waiver_type} marked verified. "
        "It will now render; check it against the statute once more before you rely on it.",
        "success",
    )
    return redirect(url_for("workflow.waivers", project_id=project_id))


# ── Compliance ──────────────────────────────────────────────────────────────


@bp.get("/compliance")
@login_required
@require_permission(COMPLIANCE_READ)
def compliance(project_id: str) -> Any:
    from massingbill.blueprints.forms import ComplianceDocumentForm, ComplianceRequirementForm

    project = _project(project_id)
    choices = [(str(k), k.label) for k in ComplianceKind]

    form = ComplianceRequirementForm()
    form.kind.choices = choices
    document_form = ComplianceDocumentForm()
    document_form.kind.choices = choices

    return render_template(
        "workflow/compliance.html",
        project=project,
        requirements=compliance_service.requirements_for(project),
        form=form,
        document_form=document_form,
        can_write=has_permission(COMPLIANCE_WRITE),
    )


@bp.post("/compliance")
@login_required
@require_permission(COMPLIANCE_WRITE)
def add_requirement(project_id: str) -> Any:
    from massingbill.blueprints.forms import ComplianceRequirementForm

    project = _project(project_id)
    form = ComplianceRequirementForm()
    form.kind.choices = [(str(k), k.label) for k in ComplianceKind]

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.compliance", project_id=project_id))

    compliance_service.add_requirement(
        project,
        ComplianceKind(form.kind.data),
        blocks_payment=bool(form.blocks_payment.data),
        actor=current_user,
    )
    db.session.commit()
    flash("Requirement added.", "success")
    return redirect(url_for("workflow.compliance", project_id=project_id))


@bp.post("/compliance/<requirement_id>/file")
@login_required
@require_permission(COMPLIANCE_WRITE)
def file_document(project_id: str, requirement_id: str) -> Any:
    from massingbill.blueprints.forms import ComplianceDocumentForm
    from massingbill.models import ComplianceRequirement

    _project(project_id)
    requirement = get_scoped_or_404(ComplianceRequirement, requirement_id)
    form = ComplianceDocumentForm()
    form.kind.choices = [(str(requirement.kind), requirement.kind_label)]

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.compliance", project_id=project_id))

    compliance_service.file_document(
        requirement,
        filename=form.reference.data or str(requirement.kind),
        effective_from=form.issued_on.data,
        expires_on=form.expires_on.data,
        actor=current_user,
    )
    db.session.commit()
    flash("Document filed.", "success")
    return redirect(url_for("workflow.compliance", project_id=project_id))


# ── Subcontracts ────────────────────────────────────────────────────────────


@bp.get("/subcontracts")
@login_required
@require_permission(SUBCONTRACT_READ)
def subcontracts(project_id: str) -> Any:
    from massingbill.blueprints.forms import RejectForm, SubApplicationForm, SubcontractForm

    project = _project(project_id)

    return render_template(
        "workflow/subcontracts.html",
        project=project,
        subcontracts=sub_service.for_project(project),
        committed=sub_service.committed_total(project),
        form=SubcontractForm(),
        billing_form=SubApplicationForm(),
        reject_form=RejectForm(),
        can_write=has_permission(SUBCONTRACT_WRITE),
    )


@bp.post("/subcontracts")
@login_required
@require_permission(SUBCONTRACT_WRITE)
def create_subcontract(project_id: str) -> Any:
    from massingbill.blueprints.forms import SubcontractForm

    project = _project(project_id)
    form = SubcontractForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.subcontracts", project_id=project_id))

    # Blank means "whatever the prime contract withholds", which is what a PM
    # expects and what keeps a sub's retainage from silently exceeding the
    # rate the owner is holding against the GC.
    retainage = form.retainage_bp.data
    if retainage is None:
        rule = _contract(project_id).retainage_rule
        retainage = rule.rate_work_bp if rule else 1000

    sub_service.create(
        project,
        number=_required(form.number.data, "Number"),
        vendor_name=_required(form.vendor_name.data, "Subcontractor"),
        amount=_required(form.amount.data, "Amount"),
        scope=form.scope.data or "",
        contact_email=form.vendor_email.data or "",
        retainage_rate_bp=retainage,
        actor=current_user,
    )
    db.session.commit()
    flash("Subcontract created.", "success")
    return redirect(url_for("workflow.subcontracts", project_id=project_id))


@bp.post("/subcontracts/<subcontract_id>/billings")
@login_required
@require_permission(SUBCONTRACT_WRITE)
def receive_billing(project_id: str, subcontract_id: str) -> Any:
    from massingbill.blueprints.forms import SubApplicationForm

    _project(project_id)
    subcontract = get_scoped_or_404(Subcontract, subcontract_id)
    form = SubApplicationForm()

    if not form.validate_on_submit():
        _flash_errors(form)
        return redirect(url_for("workflow.subcontracts", project_id=project_id))

    period_end = _required(form.period_end.data, "Through")
    sub_service.receive(
        subcontract,
        period_start=period_end.replace(day=1),
        period_end=period_end,
        completed_to_date=_required(form.amount.data, "Amount"),
        actor=current_user,
    )
    db.session.commit()
    flash("Billing recorded. Retainage was recomputed from the subcontract.", "success")
    return redirect(url_for("workflow.subcontracts", project_id=project_id))
