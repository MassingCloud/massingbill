"""Registration, sign-in, two-factor and organization switching."""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.wrappers import Response

from massingbill.extensions import db, limiter
from massingbill.models import Role, User
from massingbill.services import accounts, audit, mfa
from massingbill.services.accounts import SignInOutcome
from massingbill.services.rbac import (
    active_membership,
    clear_active_organization,
    set_active_organization,
)

bp = Blueprint("auth", __name__, url_prefix="/auth")

#: Set between password and code when two-factor is required. Holds only a user
#: id -- the user is not logged in until the code is verified.
PENDING_MFA_KEY = "pending_mfa_user_id"

#: A generic message for every failed sign-in. Saying "no such account" or
#: "wrong password" tells an attacker which addresses are registered.
GENERIC_SIGN_IN_ERROR = "Those credentials are not valid."


def _first_membership_or_none(user: User) -> str | None:
    memberships = accounts.memberships_for(user)
    return memberships[0].organization_id if memberships else None


def _finish_login(user: User, remember: bool = False) -> Response:
    accounts.complete_sign_in(user)
    login_user(user, remember=remember)
    session.pop(PENDING_MFA_KEY, None)

    organization_id = _first_membership_or_none(user)
    if organization_id:
        set_active_organization(organization_id)
        audit.record(
            organization_id,
            audit.USER_SIGNED_IN,
            entity_type="user",
            entity_id=user.id,
            actor_id=user.id,
            actor_label=user.email,
        )
    db.session.commit()
    return redirect(request.args.get("next") or url_for("projects.index"))


@bp.get("/register")
def register_form() -> Any:
    from massingbill.blueprints.forms import RegisterForm

    if current_user.is_authenticated:
        return redirect(url_for("projects.index"))
    return render_template("auth/register.html", form=RegisterForm())


@bp.post("/register")
@limiter.limit("10 per hour")
def register() -> Any:
    from massingbill.blueprints.forms import RegisterForm

    form = RegisterForm()
    if not form.validate_on_submit():
        return render_template("auth/register.html", form=form), 400

    try:
        user = accounts.create_user(
            form.email.data or "", form.password.data or "", name=form.name.data or ""
        )
        organization = accounts.create_organization(form.organization_name.data or "", user)
    except Exception:
        db.session.rollback()
        raise

    audit.record(
        organization.id,
        audit.USER_REGISTERED,
        entity_type="user",
        entity_id=user.id,
        after={"email": user.email},
        actor_id=user.id,
        actor_label=user.email,
    )
    db.session.commit()

    login_user(user)
    set_active_organization(organization.id)
    flash(f"Welcome. {organization.name} is ready.", "success")
    return redirect(url_for("projects.index"))


@bp.get("/sign-in")
def sign_in_form() -> Any:
    from massingbill.blueprints.forms import SignInForm

    if current_user.is_authenticated:
        return redirect(url_for("projects.index"))
    return render_template("auth/sign_in.html", form=SignInForm())


@bp.post("/sign-in")
@limiter.limit("20 per hour")
def sign_in() -> Any:
    from massingbill.blueprints.forms import SignInForm

    form = SignInForm()
    if not form.validate_on_submit():
        return render_template("auth/sign_in.html", form=form), 400

    result = accounts.attempt_sign_in(form.email.data or "", form.password.data or "")
    db.session.commit()

    if result.outcome == SignInOutcome.OK and result.user is not None:
        return _finish_login(result.user)

    if result.outcome == SignInOutcome.NEEDS_MFA and result.user is not None:
        session[PENDING_MFA_KEY] = result.user.id
        return redirect(url_for("auth.mfa_form"))

    if result.outcome == SignInOutcome.LOCKED:
        # Worth naming: a locked-out user who is told "bad credentials" will
        # keep trying and stay locked out.
        flash(
            "This account is temporarily locked after repeated failures. Try again shortly.",
            "error",
        )
    else:
        flash(GENERIC_SIGN_IN_ERROR, "error")

    return render_template("auth/sign_in.html", form=form), 401


@bp.get("/mfa")
def mfa_form() -> Any:
    from massingbill.blueprints.forms import MfaForm

    if PENDING_MFA_KEY not in session:
        return redirect(url_for("auth.sign_in_form"))
    return render_template("auth/mfa.html", form=MfaForm())


@bp.post("/mfa")
@limiter.limit("20 per hour")
def mfa_verify() -> Any:
    from massingbill.blueprints.forms import MfaForm

    user_id = session.get(PENDING_MFA_KEY)
    if not user_id:
        return redirect(url_for("auth.sign_in_form"))

    form = MfaForm()
    user = db.session.get(User, user_id)

    if user is None:
        session.pop(PENDING_MFA_KEY, None)
        return redirect(url_for("auth.sign_in_form"))

    if not form.validate_on_submit() or not mfa.verify_user_code(user, form.code.data or ""):
        flash("That code is not valid.", "error")
        return render_template("auth/mfa.html", form=form), 401

    return _finish_login(user)


@bp.post("/sign-out")
@login_required
def sign_out() -> Any:
    membership = active_membership()
    if membership is not None:
        audit.record(
            membership.organization_id,
            audit.USER_SIGNED_OUT,
            entity_type="user",
            entity_id=current_user.id,
            actor_id=current_user.id,
            actor_label=current_user.email,
        )
        db.session.commit()

    clear_active_organization()
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("auth.sign_in_form"))


@bp.post("/switch/<organization_id>")
@login_required
def switch_organization(organization_id: str) -> Any:
    """Change the active organization, if the user is actually a member of it."""
    allowed = {m.organization_id for m in accounts.memberships_for(current_user)}
    if organization_id not in allowed:
        # Not 403: confirming the id exists would leak another tenant.
        flash("No such organization.", "error")
        return redirect(url_for("projects.index"))

    set_active_organization(organization_id)
    return redirect(url_for("projects.index"))


# ── Two-factor enrolment ────────────────────────────────────────────────────

#: Session slot holding the not-yet-confirmed TOTP seed during enrolment.
MFA_ENROLMENT_STASH = "mfa_enrolment_pending"


@bp.get("/two-factor")
@login_required
def two_factor() -> Any:
    from massingbill.blueprints.forms import ConfirmForm, EnrolMfaForm

    if current_user.mfa_enabled:
        return render_template("auth/two_factor.html", enabled=True, form=ConfirmForm())

    secret, uri = mfa.begin_enrolment(current_user)
    session[MFA_ENROLMENT_STASH] = secret
    return render_template(
        "auth/two_factor.html",
        enabled=False,
        form=EnrolMfaForm(),
        qr_data_uri=mfa.qr_data_uri(uri),
        secret=secret,
    )


@bp.post("/two-factor")
@login_required
def enable_two_factor() -> Any:
    from massingbill.blueprints.forms import EnrolMfaForm

    form = EnrolMfaForm()
    secret = session.get(MFA_ENROLMENT_STASH)

    if not secret or not form.validate_on_submit():
        flash("Enrolment expired. Start again.", "error")
        return redirect(url_for("auth.two_factor"))

    mfa.confirm_enrolment(current_user, secret, form.code.data or "")
    session.pop(MFA_ENROLMENT_STASH, None)

    membership = active_membership()
    if membership is not None:
        audit.record(
            membership.organization_id,
            audit.MFA_ENABLED,
            entity_type="user",
            entity_id=current_user.id,
            actor_id=current_user.id,
            actor_label=current_user.email,
        )
    db.session.commit()

    flash("Two-factor authentication is on.", "success")
    return redirect(url_for("auth.two_factor"))


@bp.post("/two-factor/disable")
@login_required
def disable_two_factor() -> Any:
    from massingbill.blueprints.forms import ConfirmForm

    if not ConfirmForm().validate_on_submit():
        return redirect(url_for("auth.two_factor"))

    membership = active_membership()
    mfa.disable(current_user, membership.organization_id if membership else None)
    db.session.commit()

    flash("Two-factor authentication is off.", "success")
    return redirect(url_for("auth.two_factor"))


def register_login_manager(login_manager: Any) -> None:
    """Wire the real user loader, replacing the P0 placeholder."""

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        user = db.session.get(User, user_id)
        return user if user is not None and user.is_active else None


__all__ = ["PENDING_MFA_KEY", "Role", "bp", "register_login_manager"]
