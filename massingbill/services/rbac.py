"""Role-based access control and tenant scoping.

Two separate jobs, deliberately kept in one place so neither can be forgotten:

**Authorization** -- what a role may do, expressed as a permission set per role.
Views declare the permission they need; nothing infers it from a role name.

**Tenant scoping** -- every query on a tenant-owned table goes through
:func:`scoped`, which filters by the active organization. A cross-tenant read is
answered ``404``, not ``403``: telling someone "this exists but is not yours"
leaks the existence of another contractor's project.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from flask import abort, g, session
from flask_login import current_user
from sqlalchemy import Select, select

from massingbill.errors import ForbiddenError
from massingbill.models import Membership, Organization, Role

SESSION_ORG_KEY = "active_organization_id"

T = TypeVar("T")

# ── Permissions ─────────────────────────────────────────────────────────────

ORG_MANAGE = "org:manage"
ORG_READ = "org:read"
PROJECT_CREATE = "project:create"
PROJECT_READ = "project:read"
PROJECT_UPDATE = "project:update"
PROJECT_DELETE = "project:delete"
SOV_READ = "sov:read"
SOV_WRITE = "sov:write"
SOV_APPROVE = "sov:approve"
AUDIT_READ = "audit:read"

ALL_PERMISSIONS = frozenset(
    {
        ORG_MANAGE,
        ORG_READ,
        PROJECT_CREATE,
        PROJECT_READ,
        PROJECT_UPDATE,
        PROJECT_DELETE,
        SOV_READ,
        SOV_WRITE,
        SOV_APPROVE,
        AUDIT_READ,
    }
)

#: What each role may do. Written out in full rather than by inheritance so a
#: reader can see a role's entire authority without tracing a hierarchy -- and
#: so widening one role never silently widens another.
ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: ALL_PERMISSIONS,
    Role.ADMIN: frozenset(
        {
            ORG_MANAGE,
            ORG_READ,
            PROJECT_CREATE,
            PROJECT_READ,
            PROJECT_UPDATE,
            PROJECT_DELETE,
            SOV_READ,
            SOV_WRITE,
            SOV_APPROVE,
            AUDIT_READ,
        }
    ),
    Role.PM: frozenset(
        {
            ORG_READ,
            PROJECT_CREATE,
            PROJECT_READ,
            PROJECT_UPDATE,
            SOV_READ,
            SOV_WRITE,
            SOV_APPROVE,
        }
    ),
    # An accountant builds and edits the schedule of values but does not approve
    # it: the person who writes the numbers should not be the only person who
    # blesses them.
    Role.ACCOUNTANT: frozenset({ORG_READ, PROJECT_READ, SOV_READ, SOV_WRITE}),
    Role.VIEWER: frozenset({ORG_READ, PROJECT_READ, SOV_READ}),
    # Counterparties. They reach documents through scoped links, not the app.
    Role.EXTERNAL_APPROVER: frozenset({PROJECT_READ, SOV_READ}),
    Role.SUB_CONTACT: frozenset(),
}


def permissions_for(role: Role) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


# ── Active organization ─────────────────────────────────────────────────────


def set_active_organization(organization_id: str) -> None:
    session[SESSION_ORG_KEY] = organization_id
    g.pop("active_membership", None)


def clear_active_organization() -> None:
    session.pop(SESSION_ORG_KEY, None)
    g.pop("active_membership", None)


def register_request_hooks(app: Any) -> None:
    """Clear the per-request membership cache at the start of every request.

    ``g`` is bound to the *application* context, not the request, and a
    long-lived application context (a CLI command, a worker, or a test that
    holds one open) would otherwise carry a stale membership from one request
    into the next -- so a revoked role would keep working. Clearing explicitly
    makes the cache request-scoped regardless of how the context is managed.
    """

    @app.before_request
    def _reset_membership_cache() -> None:
        g.pop("active_membership", None)


def active_membership() -> Membership | None:
    """The signed-in user's membership in the organization they are working in.

    Re-read per request and cached on ``g``: a role change must take effect on
    the next request, not at the next sign-in.
    """
    if "active_membership" in g:
        cached: Membership | None = g.active_membership
        return cached

    membership: Membership | None = None
    organization_id = session.get(SESSION_ORG_KEY)

    if organization_id and getattr(current_user, "is_authenticated", False):
        from massingbill.extensions import db

        membership = db.session.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == current_user.id,
            )
        )

    g.active_membership = membership
    return membership


def active_organization() -> Organization | None:
    membership = active_membership()
    return membership.organization if membership else None


def active_organization_id() -> str | None:
    membership = active_membership()
    return membership.organization_id if membership else None


def require_organization_id() -> str:
    """The active organization id, or a 401.

    Views behind ``require_permission`` already have one, but they still need a
    non-optional value to pass along. An ``assert`` would do it in development
    and vanish under ``python -O`` -- leaving ``None`` to flow into an
    organization_id column. This raises either way.
    """
    organization_id = active_organization_id()
    if organization_id is None:
        abort(401)
    return organization_id


def has_permission(permission: str) -> bool:
    membership = active_membership()
    if membership is None:
        return False
    return permission in permissions_for(Role(membership.role))


def require_permission(permission: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Guard a view. Requires an active organization *and* the permission.

    Raises 401 when there is no active organization (the user has not chosen
    one, or their membership was revoked) and 403 when the role is insufficient.
    """
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission!r}")

    def decorator(view: Callable[..., T]) -> Callable[..., T]:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            membership = active_membership()
            if membership is None:
                abort(401)
            if permission not in permissions_for(Role(membership.role)):
                raise ForbiddenError(
                    f"Your role ({Role(membership.role).label}) cannot perform this action."
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator


# ── Tenant scoping ──────────────────────────────────────────────────────────


def scoped(model: type[Any], organization_id: str | None = None) -> Select[Any]:
    """A SELECT already filtered to the active organization.

    Every read of a tenant-owned table starts here. The alternative -- expecting
    each query to remember its own filter -- fails the first time somebody adds
    a view in a hurry.
    """
    org_id = organization_id or active_organization_id()
    if org_id is None:
        # No active organization means nothing is visible. Returning an
        # impossible filter rather than raising keeps callers simple and fails
        # closed either way.
        return select(model).where(model.organization_id.is_(None))
    return select(model).where(model.organization_id == org_id)


def get_scoped_or_404(model: type[Any], entity_id: str) -> Any:
    """Fetch one row from the active organization, or 404.

    404 rather than 403 on a cross-tenant read: "this exists but is not yours"
    tells one contractor that another contractor's project id is real.
    """
    from massingbill.extensions import db

    entity = db.session.scalar(scoped(model).where(model.id == entity_id))
    if entity is None:
        abort(404)
    return entity
