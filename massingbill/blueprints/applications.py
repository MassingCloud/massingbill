"""The monthly requisition, as a screen.

One worksheet per period. The G703 rows are the only editable thing; every G702
header line is derived, and is shown read-only next to the reconciliation so a
project accountant can see what changed and whether it still balances *before*
the owner does.

Nothing here computes money. Every figure comes from ``services/application``
and every check from ``services/tieout`` -- the view's whole job is to collect
what somebody typed and show them what the engine made of it.
"""

from __future__ import annotations

from typing import Any, TypeVar

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from massingbill.errors import NotFoundError, ValidationError
from massingbill.extensions import db
from massingbill.models import (
    Application,
    PaymentMethod,
    PrimeContract,
    Project,
    StorageLocation,
    StoredMaterial,
)
from massingbill.services import application as app_service
from massingbill.services import compliance as compliance_service
from massingbill.services import payments as payment_service
from massingbill.services import sov as sov_service
from massingbill.services import tieout
from massingbill.services import waivers as waiver_service
from massingbill.services.money import cents
from massingbill.services.rbac import (
    APPLICATION_CERTIFY,
    APPLICATION_READ,
    APPLICATION_SUBMIT,
    APPLICATION_WRITE,
    PAYMENT_READ,
    PAYMENT_WRITE,
    get_scoped_or_404,
    has_permission,
    require_permission,
)

bp = Blueprint("applications", __name__, url_prefix="/projects/<project_id>/applications")

T = TypeVar("T")


def _required(value: T | None, field: str) -> T:
    """Narrow a field that validation has already required.

    ``validate_on_submit`` has enforced requiredness by the time these run,
    but WTForms types every field as optional, so the narrowing has to happen
    somewhere. Raising rather than asserting keeps the check under ``-O`` --
    a ``None`` reaching the money path is worth a 400, not a ``TypeError``
    three frames down.
    """
    if value is None:
        raise ValidationError(f"{field} is required.")
    return value


def _contract_for(project_id: str) -> PrimeContract:
    project = get_scoped_or_404(Project, project_id)
    if project.prime_contract is None:
        raise NotFoundError("This project has no prime contract yet.")
    return project.prime_contract


def _application_for(project_id: str, application_id: str) -> Application:
    """Fetch within this project, not merely within this tenant.

    ``get_scoped_or_404`` already stops cross-tenant reads. This additionally
    stops a *correct* id from the wrong project rendering under this project's
    heading, which would be a quietly wrong page rather than an error.
    """
    contract = _contract_for(project_id)
    application = get_scoped_or_404(Application, application_id)
    if application.prime_contract_id != contract.id:
        raise NotFoundError("That application belongs to a different project.")
    return application


@bp.get("/")
@login_required
@require_permission(APPLICATION_READ)
def index(project_id: str) -> Any:
    from massingbill.blueprints.forms import OpenPeriodForm

    contract = _contract_for(project_id)
    applications = app_service.applications_for(contract)

    return render_template(
        "applications/index.html",
        project=contract.project,
        contract=contract,
        applications=applications,
        open_application=app_service.open_application(contract),
        totals=app_service.totals_check(contract),
        form=OpenPeriodForm(),
        can_write=has_permission(APPLICATION_WRITE),
    )


@bp.post("/open")
@login_required
@require_permission(APPLICATION_WRITE)
def open_period(project_id: str) -> Any:
    from massingbill.blueprints.forms import OpenPeriodForm

    contract = _contract_for(project_id)
    form = OpenPeriodForm()

    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
        return redirect(url_for("applications.index", project_id=project_id))

    application = app_service.open_period(
        contract,
        period_start=_required(form.period_start.data, "Period start"),
        period_end=_required(form.period_end.data, "Period end"),
        application_date=form.application_date.data,
        actor=current_user,
    )
    db.session.commit()
    flash(f"Application #{application.number} is open.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application.id)
    )


@bp.get("/<application_id>")
@login_required
@require_permission(APPLICATION_READ)
def show(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import (
        CertifyForm,
        ConfirmForm,
        PaymentForm,
        PeriodLineForm,
        VoidForm,
    )

    application = _application_for(project_id, application_id)

    # One form per row, prefixed by line id, so each row owns its own error.
    line_forms = {
        line.id: PeriodLineForm(
            prefix=f"line-{line.id}",
            data={"this_period": line.col_e_this_period, "stored": line.col_f_stored},
        )
        for line in application.lines
    }

    certify_form = CertifyForm()
    if not certify_form.is_submitted():
        # Pre-fill with what was asked for, so certifying in full is one click
        # and a *difference* is something somebody had to type.
        certify_form.amount_certified.data = cents(application.line8_current_payment_due)

    return render_template(
        "applications/show.html",
        project=application.prime_contract.project,
        contract=application.prime_contract,
        application=application,
        line_forms=line_forms,
        report=tieout.run(application),
        waivers=waiver_service.for_application(application),
        compliance=compliance_service.evaluate(application),
        payments_total=payment_service.paid_to_date(application),
        payment_variance=payment_service.variance(application),
        certify_form=certify_form,
        void_form=VoidForm(),
        payment_form=PaymentForm(),
        confirm_form=ConfirmForm(),
        can_write=has_permission(APPLICATION_WRITE),
        can_submit=has_permission(APPLICATION_SUBMIT),
        can_certify=has_permission(APPLICATION_CERTIFY),
        can_pay=has_permission(PAYMENT_WRITE),
        can_see_payments=has_permission(PAYMENT_READ),
    )


@bp.post("/<application_id>/enter")
@login_required
@require_permission(APPLICATION_WRITE)
def enter(project_id: str, application_id: str) -> Any:
    """Save the typed G703 rows.

    Every row is validated before *any* row is saved. A partial save on a pay
    application would leave a period that looks entered and is not, and the
    person who would find out is the owner.
    """
    from massingbill.blueprints.forms import PeriodLineForm

    application = _application_for(project_id, application_id)
    if not application.is_editable:
        raise ValidationError("This application is frozen and cannot be edited.")

    entries: list[app_service.PeriodEntry] = []
    problems = False

    for line in application.lines:
        form = PeriodLineForm(prefix=f"line-{line.id}")
        if not form.validate():
            problems = True
            for field, errors in form.errors.items():
                for error in errors:
                    flash(f"Line {line.item_no} ({field.replace('_', ' ')}): {error}", "error")
            continue
        entries.append(
            app_service.PeriodEntry(
                line_id=line.id,
                this_period=form.this_period.data or cents(0),
                stored=form.stored.data or cents(0),
            )
        )

    if problems:
        db.session.rollback()
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    app_service.enter(application, entries, actor=current_user)
    db.session.commit()
    flash("Saved.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application_id)
    )


@bp.post("/<application_id>/submit")
@login_required
@require_permission(APPLICATION_SUBMIT)
def submit(project_id: str, application_id: str) -> Any:
    application = _application_for(project_id, application_id)

    try:
        app_service.submit(application, actor=current_user)
    except ValidationError as exc:
        db.session.rollback()
        # The findings are the useful part, so surface them individually rather
        # than as one paragraph nobody reads to the end of.
        flash(exc.message, "error")
        for finding in exc.details.get("findings", []):
            flash(f"{finding['rule_id']}: {finding['message']}", "error")
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    db.session.commit()
    flash(f"Application #{application.number} submitted.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application_id)
    )


@bp.post("/<application_id>/certify")
@login_required
@require_permission(APPLICATION_CERTIFY)
def certify(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import CertifyForm

    application = _application_for(project_id, application_id)
    form = CertifyForm()

    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    amount = _required(form.amount_certified.data, "Amount certified")
    if amount != application.line8_current_payment_due and not form.reason.data:
        flash(
            "The certified amount differs from the amount applied for. "
            "Record why -- that difference is the thing everyone will ask about.",
            "error",
        )
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    app_service.certify(
        application,
        amount,
        certified_by_label=_required(form.certified_by_label.data, "Certified by"),
        reason=form.reason.data or "",
        actor=current_user,
    )
    db.session.commit()
    flash("Certificate recorded.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application_id)
    )


@bp.post("/<application_id>/pay")
@login_required
@require_permission(PAYMENT_WRITE)
def pay(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import PaymentForm

    application = _application_for(project_id, application_id)
    form = PaymentForm()

    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    payment_service.record(
        application,
        amount=_required(form.amount.data, "Amount"),
        received_on=_required(form.received_on.data, "Received on"),
        method=PaymentMethod(form.method.data),
        reference=form.reference.data or "",
        joint_payee=form.joint_payee.data or "",
        note=form.note.data or "",
        actor=current_user,
    )
    db.session.commit()
    flash("Payment recorded.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application_id)
    )


@bp.post("/<application_id>/void")
@login_required
@require_permission(APPLICATION_WRITE)
def void(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import VoidForm

    application = _application_for(project_id, application_id)
    form = VoidForm()

    if not form.validate_on_submit():
        flash("A void needs a reason.", "error")
        return redirect(
            url_for("applications.show", project_id=project_id, application_id=application_id)
        )

    app_service.void(application, reason=_required(form.reason.data, "Reason"), actor=current_user)
    db.session.commit()
    flash(f"Application #{application.number} voided.", "success")
    return redirect(url_for("applications.index", project_id=project_id))


# ── Stored materials ────────────────────────────────────────────────────────


@bp.get("/<application_id>/materials")
@login_required
@require_permission(APPLICATION_READ)
def materials(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm, StoredMaterialForm

    application = _application_for(project_id, application_id)
    schedule = sov_service.approved_schedule(application.prime_contract)

    form = StoredMaterialForm()
    form.sov_line_id.choices = [
        (line.id, f"{line.item_no} — {line.description}")
        for line in (schedule.lines if schedule else [])
    ]

    stored = db.session.scalars(
        db.select(StoredMaterial).where(
            StoredMaterial.organization_id == application.organization_id
        )
    )

    return render_template(
        "applications/materials.html",
        project=application.prime_contract.project,
        application=application,
        materials=list(stored),
        form=form,
        confirm_form=ConfirmForm(),
        can_write=has_permission(APPLICATION_WRITE),
    )


@bp.post("/<application_id>/materials")
@login_required
@require_permission(APPLICATION_WRITE)
def add_material(project_id: str, application_id: str) -> Any:
    from massingbill.blueprints.forms import StoredMaterialForm

    application = _application_for(project_id, application_id)
    form = StoredMaterialForm()
    form.sov_line_id.choices = [(request.form.get("sov_line_id", ""), "")]

    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
        return redirect(
            url_for("applications.materials", project_id=project_id, application_id=application_id)
        )

    db.session.add(
        StoredMaterial(
            organization_id=application.organization_id,
            sov_line_id=_required(form.sov_line_id.data, "Schedule line"),
            description=_required(form.description.data, "Description"),
            location=StorageLocation(form.location.data),
            value_cents=int(_required(form.value.data, "Value")),
            supplier=form.supplier.data or "",
            invoice_ref=form.invoice_ref.data or "",
            bond_ref=form.bond_ref.data or "",
        )
    )
    db.session.commit()
    flash("Material recorded. It appears in column F on the next recompute.", "success")
    return redirect(
        url_for("applications.materials", project_id=project_id, application_id=application_id)
    )


@bp.post("/<application_id>/materials/apply")
@login_required
@require_permission(APPLICATION_WRITE)
def apply_materials(project_id: str, application_id: str) -> Any:
    """Pull stored material into column F, and install what has arrived."""
    application = _application_for(project_id, application_id)
    app_service.apply_stored_materials(application)
    db.session.commit()
    flash("Stored materials applied.", "success")
    return redirect(
        url_for("applications.show", project_id=project_id, application_id=application_id)
    )
