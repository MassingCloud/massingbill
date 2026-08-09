"""``/api/massingbill/v1`` -- the machine-facing surface.

Shaped to massing's REST conventions (SPEC.md 3.1) so a client written against
one reads the other: resource nouns, ``Authorization: Bearer <key>`` with an
``X-Api-Key`` alternative, and the same error-code table -- 401 bad secret,
403 out of scope, 404 not found, 409 state refused, 429 rate limited.

Authentication resolves an :class:`~massingbill.services.rbac.ApiPrincipal` onto
``g``, after which *every* existing authorization helper works unchanged:
``scoped()`` filters by organization, ``require_permission`` checks the key's
scopes. There is deliberately no API-specific tenant filter -- a second copy of
that logic is how the two drift and one of them starts leaking.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from flask import Blueprint, Response, current_app, g, jsonify, request

from massingbill.errors import AuthenticationError, NotFoundError, ValidationError
from massingbill.extensions import csrf, db, limiter
from massingbill.models import (
    Application,
    ChangeOrder,
    PrimeContract,
    Project,
    ScheduleOfValues,
)
from massingbill.services import apikeys, rbac, tieout
from massingbill.services import application as app_service
from massingbill.services.money import Cents, to_decimal
from massingbill.services.renderers.documents import Format, available_formats, render

bp = Blueprint("api", __name__, url_prefix="/api/massingbill/v1")

T = TypeVar("T")

#: Bounds on a page. A caller that asks for everything gets the maximum rather
#: than an error -- an integration should degrade to slower, not to broken.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# ── Authentication ──────────────────────────────────────────────────────────


def _presented_token() -> str:
    """The key from either header massing accepts."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-Api-Key", "").strip()


@bp.before_request
def _authenticate() -> None:
    """Resolve the key, or refuse the request.

    One message for every failure mode. "No such key" and "wrong secret" are
    the same answer here, because telling them apart tells an attacker which
    half of a guessed token was right.
    """
    token = _presented_token()
    if not token:
        raise AuthenticationError(
            "Send an API key as 'Authorization: Bearer <key>' or 'X-Api-Key: <key>'."
        )

    key = apikeys.authenticate(token)
    if key is None:
        raise AuthenticationError("That API key is not valid.")

    rbac.set_api_principal(apikeys.principal_for(key))
    g.api_key = key


@bp.after_request
def _settle(response: Response) -> Response:
    """Commit the coarsened ``last_used_at``, or discard a failed request.

    Read endpoints do not otherwise commit, so without this the only record of
    a key being used would be dropped at the end of the request.

    The rollback half matters more. The error handlers render a message but do
    **not** roll back, so an endpoint that mutated and then raised would have
    its half-finished work committed here -- this hook is the only thing
    standing between a refused write and a partial one.
    """
    if getattr(g, "api_key", None) is None:
        return response

    if response.status_code >= 400:
        db.session.rollback()
    else:
        db.session.commit()
    return response


def _rate_limit() -> str:
    """This key's own ceiling, or the deployment default."""
    key = getattr(g, "api_key", None)
    if key is not None and key.rate_limit_per_minute:
        return f"{key.rate_limit_per_minute} per minute"
    limit: str = current_app.config["MASSINGBILL_SETTINGS"].api_rate_limit_default
    return limit


def _rate_limit_key() -> str:
    """Rate-limit per API key, falling back to the caller's address.

    Limiting by address would make one busy customer throttle every other
    customer behind the same NAT.
    """
    key = getattr(g, "api_key", None)
    if key is not None:
        return f"apikey:{key.id}"
    return request.remote_addr or "anonymous"


# ── Envelope ────────────────────────────────────────────────────────────────


def ok(data: Any, **extra: Any) -> Response:
    payload: dict[str, Any] = {"data": data}
    payload.update(extra)
    return jsonify(payload)


def paginated(items: list[Any], total: int, page: int, per_page: int) -> Response:
    return ok(
        items,
        meta={
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, -(-total // per_page)),
        },
    )


def _page_args() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", DEFAULT_PAGE_SIZE))
    except ValueError as exc:
        raise ValidationError("page and per_page must be integers.") from exc
    return page, max(1, min(per_page, MAX_PAGE_SIZE))


def scope(permission: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Require a scope. Thin alias for the shared decorator, named for readers."""
    return rbac.require_permission(permission)


def money(value: int | None) -> dict[str, Any] | None:
    """Every amount, in both forms.

    ``cents`` is authoritative and is what a client should compute with;
    ``amount`` is the same number as a decimal string for humans and for
    systems that will not accept integer minor units. Never a float: the whole
    product rests on that.
    """
    if value is None:
        return None
    return {"cents": int(value), "amount": str(to_decimal(Cents(int(value)))), "currency": "USD"}


# ── Serializers ─────────────────────────────────────────────────────────────


def _project(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "number": project.number,
        "name": project.name,
        "status": str(project.status),
        "jurisdiction_state": project.jurisdiction_state,
        "created_at": project.created_at.isoformat(),
    }


def _contract(contract: PrimeContract) -> dict[str, Any]:
    return {
        "id": contract.id,
        "project_id": contract.project_id,
        "original_contract_sum": money(contract.original_contract_sum_cents),
        "retainage_mode": str(contract.retainage_rule.mode) if contract.retainage_rule else None,
    }


def _application(application: Application, *, lines: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": application.id,
        "number": application.number,
        "status": str(application.status),
        "period_start": application.period_start.isoformat(),
        "period_end": application.period_end.isoformat(),
        # Named for the G702 line they are, because that is what a reader of
        # the form will be reconciling against.
        "line1_original_contract_sum": money(application.line1_original_sum),
        "line2_net_change_orders": money(application.line2_net_co),
        "line3_contract_sum_to_date": money(application.line3_contract_sum_to_date),
        "line4_completed_stored": money(application.line4_completed_stored),
        "line5_retainage": money(application.line5_total_retainage),
        "line5a_retainage_work": money(application.line5a_retainage_work),
        "line5b_retainage_stored": money(application.line5b_retainage_stored),
        "line6_total_earned_less_retainage": money(application.line6_earned_less_retainage),
        "line7_previous_certificates": money(application.line7_previous_certificates),
        "line8_current_payment_due": money(application.line8_current_payment_due),
        "line9_balance_to_finish": money(application.line9_balance_to_finish),
        "certified_payment": money(application.certified_payment_cents),
    }
    if lines:
        payload["lines"] = [_application_line(line) for line in application.lines]
    return payload


def _application_line(line: Any) -> dict[str, Any]:
    """G703 columns A-I, under the names the continuation sheet uses."""
    return {
        "item_no": line.item_no,
        "description": line.description,
        "col_c_scheduled_value": money(line.col_c_scheduled_value),
        "col_d_previous": money(line.col_d_previous),
        "col_e_this_period": money(line.col_e_this_period),
        "col_f_stored": money(line.col_f_stored),
        "col_g_completed_stored": money(line.col_g_completed_stored),
        "col_h_balance_to_finish": money(line.col_h_balance_to_finish),
        "col_i_retainage": money(line.col_i_retainage),
    }


def _finding(finding: Any) -> dict[str, Any]:
    return {
        "rule_id": finding.rule_id,
        "severity": str(finding.severity),
        "message": finding.message,
        "citation": getattr(finding, "citation", ""),
    }


# ── Routes ──────────────────────────────────────────────────────────────────


@bp.get("/status")
def status() -> Response:
    """Cheap call that proves a key works and says what it may do.

    Deliberately requires no scope beyond being a valid key. A narrowly scoped
    key still needs to be able to answer "am I alive and what am I allowed to
    do" -- making that call itself require a scope is how an integrator ends up
    debugging a 403 that tells them nothing.
    """
    principal = rbac.current_api_principal()
    if principal is None:  # pragma: no cover - before_request guarantees it
        raise AuthenticationError("That API key is not valid.")
    return ok(
        {
            "organization_id": principal.organization_id,
            "scopes": sorted(principal.scopes),
            "formats": [str(fmt) for fmt in available_formats()],
        }
    )


@bp.get("/projects")
@scope(rbac.PROJECT_READ)
def list_projects() -> Response:
    page, per_page = _page_args()
    query = rbac.scoped(Project).order_by(Project.created_at.desc())
    total = _count(query)
    rows = db.session.scalars(query.offset((page - 1) * per_page).limit(per_page))
    return paginated([_project(p) for p in rows], total, page, per_page)


@bp.get("/projects/<project_id>")
@scope(rbac.PROJECT_READ)
def get_project(project_id: str) -> Response:
    project = rbac.get_scoped_or_404(Project, project_id)
    contracts = db.session.scalars(
        rbac.scoped(PrimeContract).where(PrimeContract.project_id == project.id)
    )
    return ok(_project(project) | {"contracts": [_contract(c) for c in contracts]})


@bp.get("/projects/<project_id>/schedule-of-values")
@scope(rbac.SOV_READ)
def get_schedule(project_id: str) -> Response:
    project = rbac.get_scoped_or_404(Project, project_id)
    schedule = db.session.scalar(
        rbac.scoped(ScheduleOfValues)
        .join(PrimeContract)
        .where(PrimeContract.project_id == project.id)
        .order_by(ScheduleOfValues.revision.desc())
    )
    if schedule is None:
        raise NotFoundError("This project has no schedule of values yet.")

    return ok(
        {
            "id": schedule.id,
            "revision": schedule.revision,
            "status": str(schedule.status),
            "lines": [
                {
                    "item_no": line.item_no,
                    "description": line.description,
                    "csi_code": line.csi_code,
                    "scheduled_value": money(line.scheduled_value_cents),
                }
                for line in schedule.lines
            ],
        }
    )


@bp.get("/applications")
@scope(rbac.APPLICATION_READ)
def list_applications() -> Response:
    page, per_page = _page_args()
    query = rbac.scoped(Application)

    if project_id := request.args.get("project_id"):
        query = query.join(PrimeContract).where(PrimeContract.project_id == project_id)
    if status_filter := request.args.get("status"):
        query = query.where(Application.status == status_filter)

    query = query.order_by(Application.number.desc())
    total = _count(query)
    rows = db.session.scalars(query.offset((page - 1) * per_page).limit(per_page))
    return paginated([_application(a) for a in rows], total, page, per_page)


@bp.get("/applications/<application_id>")
@scope(rbac.APPLICATION_READ)
def get_application(application_id: str) -> Response:
    application = rbac.get_scoped_or_404(Application, application_id)
    return ok(_application(application, lines=True))


@bp.get("/applications/<application_id>/tieout")
@scope(rbac.APPLICATION_READ)
def get_tieout(application_id: str) -> Response:
    """The reconciliation, as data.

    The reason the API exists at all: an integration can ask "does this
    balance, and if not, which rule failed" without parsing a PDF.
    """
    application = rbac.get_scoped_or_404(Application, application_id)
    report = tieout.run(application)
    return ok(
        {
            "ok": report.ok,
            "summary": report.summary(),
            "findings": [_finding(f) for f in report.findings],
        }
    )


@bp.get("/applications/<application_id>/document.<fmt>")
@scope(rbac.APPLICATION_READ)
def get_document(application_id: str, fmt: str) -> Response:
    application = rbac.get_scoped_or_404(Application, application_id)
    try:
        chosen = Format(fmt)
    except ValueError as exc:
        raise ValidationError(
            f"Unknown format {fmt!r}.",
            details={"available": [str(f) for f in available_formats()]},
        ) from exc

    document = render(application, chosen)
    response = current_app.response_class(document.content, mimetype=document.content_type)
    response.headers["Content-Disposition"] = f'attachment; filename="{document.filename}"'
    return response


@bp.get("/change-orders")
@scope(rbac.PROJECT_READ)
def list_change_orders() -> Response:
    page, per_page = _page_args()
    query = rbac.scoped(ChangeOrder).order_by(ChangeOrder.created_at.desc())
    total = _count(query)
    rows = db.session.scalars(query.offset((page - 1) * per_page).limit(per_page))
    return paginated(
        [
            {
                "id": co.id,
                "number": co.number,
                "description": co.description,
                "status": str(co.status),
                "amount": money(co.amount_cents),
                "approved_date": co.approved_date.isoformat() if co.approved_date else None,
            }
            for co in rows
        ],
        total,
        page,
        per_page,
    )


# ── Writes ──────────────────────────────────────────────────────────────────


@bp.post("/applications/<application_id>/submit")
@scope(rbac.APPLICATION_SUBMIT)
def submit_application(application_id: str) -> Response:
    """Freeze and submit. Refuses on a blocking tie-out finding, like the UI.

    The API does not get a quieter version of the rules. An integration that
    could submit an application the interface would have refused is a way to
    launder a bad number into the owner's inbox.
    """
    application = rbac.get_scoped_or_404(Application, application_id)
    key = g.api_key
    app_service.submit(application, actor_label=f"api key {key.masked} ({key.name})")
    db.session.commit()
    return ok(_application(application))


def _count(query: Any) -> int:
    return int(db.session.scalar(db.select(db.func.count()).select_from(query.subquery())) or 0)


def register_api(app: Any) -> None:
    """Attach the blueprint with the exemptions machine clients need.

    CSRF protection is for cookie-authenticated browsers; a Bearer key carries
    no ambient authority for a third-party page to abuse, and leaving CSRF on
    would simply make the API unusable.
    """
    csrf.exempt(bp)
    limiter.limit(_rate_limit, key_func=_rate_limit_key)(bp)
    app.register_blueprint(bp)
