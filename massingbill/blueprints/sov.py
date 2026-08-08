"""Building, approving and revising the schedule of values."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from massingbill.errors import NotFoundError
from massingbill.extensions import db
from massingbill.models import PrimeContract, Project, ScheduleOfValues
from massingbill.services import sov as sov_service
from massingbill.services.money import cents
from massingbill.services.rbac import (
    SOV_APPROVE,
    SOV_READ,
    SOV_WRITE,
    get_scoped_or_404,
    has_permission,
    require_permission,
)

bp = Blueprint("sov", __name__, url_prefix="/projects/<project_id>/sov")


def _contract_for(project_id: str) -> PrimeContract:
    project = get_scoped_or_404(Project, project_id)
    if project.prime_contract is None:
        raise NotFoundError("This project has no prime contract yet. Add the contract sum first.")
    return project.prime_contract


def _schedule_for(project_id: str) -> ScheduleOfValues:
    contract = _contract_for(project_id)
    schedule = sov_service.current_schedule(contract)
    if schedule is None:
        raise NotFoundError("This contract has no schedule of values yet.")
    return schedule


@bp.get("/")
@login_required
@require_permission(SOV_READ)
def show(project_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm, SovLineForm

    contract = _contract_for(project_id)
    schedule = sov_service.current_schedule(contract)

    return render_template(
        "sov/show.html",
        project=contract.project,
        contract=contract,
        schedule=schedule,
        reconciliation=sov_service.reconciliation(schedule) if schedule else None,
        line_form=SovLineForm(),
        confirm_form=ConfirmForm(),
        can_write=has_permission(SOV_WRITE),
        can_approve=has_permission(SOV_APPROVE),
    )


@bp.post("/create")
@login_required
@require_permission(SOV_WRITE)
def create(project_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    contract = _contract_for(project_id)
    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("sov.show", project_id=project_id))

    sov_service.create_schedule(contract, actor=current_user)
    db.session.commit()

    flash("Schedule of values started.", "success")
    return redirect(url_for("sov.show", project_id=project_id))


@bp.post("/lines")
@login_required
@require_permission(SOV_WRITE)
def add_line(project_id: str) -> Any:
    from massingbill.blueprints.forms import SovLineForm

    schedule = _schedule_for(project_id)
    form = SovLineForm()

    if not form.validate_on_submit():
        flash("Check the line details and try again.", "error")
        return redirect(url_for("sov.show", project_id=project_id))

    sov_service.add_line(
        schedule,
        sov_service.LineInput(
            item_no=form.item_no.data or "",
            description=form.description.data or "",
            scheduled_value_cents=cents(int(form.scheduled_value.data or 0)),
            csi_code=form.csi_code.data or "",
            group=form.group.data or "",
            retainage_rate_bp=form.retainage_rate.data,
            is_general_conditions=bool(form.is_general_conditions.data),
            is_allowance=bool(form.is_allowance.data),
        ),
        actor=current_user,
    )
    db.session.commit()

    flash("Line added.", "success")
    return redirect(url_for("sov.show", project_id=project_id))


@bp.get("/lines/<line_id>")
@login_required
@require_permission(SOV_WRITE)
def edit_line(project_id: str, line_id: str) -> Any:
    from massingbill.blueprints.forms import SovLineForm

    schedule = _schedule_for(project_id)
    line = sov_service.get_line(schedule, line_id)

    form = SovLineForm(obj=line)
    form.scheduled_value.data = cents(line.base_scheduled_value_cents)
    form.retainage_rate.data = line.retainage_rate_bp

    return render_template(
        "sov/edit_line.html", form=form, line=line, project=schedule.prime_contract.project
    )


@bp.post("/lines/<line_id>")
@login_required
@require_permission(SOV_WRITE)
def update_line(project_id: str, line_id: str) -> Any:
    from massingbill.blueprints.forms import SovLineForm

    schedule = _schedule_for(project_id)
    line = sov_service.get_line(schedule, line_id)
    form = SovLineForm()

    if not form.validate_on_submit():
        return (
            render_template(
                "sov/edit_line.html",
                form=form,
                line=line,
                project=schedule.prime_contract.project,
            ),
            400,
        )

    sov_service.update_line(
        line,
        sov_service.LineInput(
            item_no=form.item_no.data or "",
            description=form.description.data or "",
            scheduled_value_cents=cents(int(form.scheduled_value.data or 0)),
            csi_code=form.csi_code.data or "",
            group=form.group.data or "",
            retainage_rate_bp=form.retainage_rate.data,
            is_general_conditions=bool(form.is_general_conditions.data),
            is_allowance=bool(form.is_allowance.data),
        ),
        actor=current_user,
    )
    db.session.commit()

    flash("Line saved.", "success")
    return redirect(url_for("sov.show", project_id=project_id))


@bp.post("/lines/<line_id>/remove")
@login_required
@require_permission(SOV_WRITE)
def remove_line(project_id: str, line_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    schedule = _schedule_for(project_id)
    line = sov_service.get_line(schedule, line_id)

    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("sov.show", project_id=project_id))

    sov_service.remove_line(line, actor=current_user)
    db.session.commit()

    flash("Line removed.", "success")
    return redirect(url_for("sov.show", project_id=project_id))


@bp.post("/approve")
@login_required
@require_permission(SOV_APPROVE)
def approve(project_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    schedule = _schedule_for(project_id)
    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("sov.show", project_id=project_id))

    sov_service.approve(schedule, actor=current_user)
    db.session.commit()

    flash(f"Revision {schedule.revision} approved.", "success")
    return redirect(url_for("sov.show", project_id=project_id))


@bp.post("/revise")
@login_required
@require_permission(SOV_APPROVE)
def revise(project_id: str) -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    schedule = _schedule_for(project_id)
    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("sov.show", project_id=project_id))

    revision = sov_service.create_revision(schedule, actor=current_user)
    db.session.commit()

    flash(f"Revision {revision.revision} created.", "success")
    return redirect(url_for("sov.show", project_id=project_id))
