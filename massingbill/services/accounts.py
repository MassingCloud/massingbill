"""User, organization and membership operations.

Sign-in policy lives here rather than in the view so it is testable without a
request: lockout after repeated failures, a uniform response whether or not the
account exists, and a counter that resets on success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache

from slugify import slugify
from sqlalchemy import func, select

from massingbill.errors import ConflictError, ValidationError
from massingbill.extensions import db
from massingbill.models import Membership, Organization, Role, User
from massingbill.models.base import utcnow
from massingbill.security import hash_password, needs_rehash, verify_password
from massingbill.services import audit

#: Failures before an account is locked, and for how long. Chosen so a
#: forgetful user is inconvenienced for a minute or two while an attacker
#: gets roughly a hundred attempts a day.
MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

MIN_PASSWORD_LENGTH = 12


class SignInOutcome(StrEnum):
    """Why a sign-in attempt ended the way it did."""

    OK = "ok"
    NEEDS_MFA = "needs_mfa"
    BAD_CREDENTIALS = "bad_credentials"
    LOCKED = "locked"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class SignInResult:
    outcome: SignInOutcome
    user: User | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == SignInOutcome.OK


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    """Length only, deliberately.

    NIST SP 800-63B advises length over composition rules: forcing a symbol
    produces ``Password1!`` and a sticky note. Breach-corpus checking belongs
    here later; a composition rule does not.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def get_user_by_email(email: str) -> User | None:
    return db.session.scalar(select(User).where(User.email == normalize_email(email)))


def create_user(email: str, password: str, *, name: str = "") -> User:
    email = normalize_email(email)
    validate_password(password)

    if get_user_by_email(email) is not None:
        raise ConflictError("An account with that email address already exists.")

    user = User(email=email, name=name, password_hash=hash_password(password))
    db.session.add(user)
    db.session.flush()
    return user


def create_organization(name: str, owner: User) -> Organization:
    slug = slugify(name)[:80] or "org"
    if db.session.scalar(select(Organization).where(Organization.slug == slug)) is not None:
        # Slugs are user-visible; disambiguate rather than silently reusing.
        suffix = db.session.scalar(
            select(func.count()).select_from(Organization).where(Organization.slug.like(f"{slug}%"))
        )
        slug = f"{slug}-{(suffix or 0) + 1}"[:80]

    organization = Organization(name=name.strip(), slug=slug)
    db.session.add(organization)
    db.session.flush()

    add_member(organization, owner, Role.OWNER, actor=owner)
    audit.record(
        organization.id,
        audit.ORG_CREATED,
        entity_type="organization",
        entity_id=organization.id,
        after={"name": organization.name, "slug": organization.slug},
        actor_id=owner.id,
        actor_label=owner.email,
    )
    return organization


def add_member(
    organization: Organization, user: User, role: Role, *, actor: User | None = None
) -> Membership:
    existing = db.session.scalar(
        select(Membership).where(
            Membership.organization_id == organization.id, Membership.user_id == user.id
        )
    )
    if existing is not None:
        raise ConflictError("That user is already a member of this organization.")

    membership = Membership(organization_id=organization.id, user_id=user.id, role=role)
    db.session.add(membership)
    db.session.flush()

    audit.record(
        organization.id,
        audit.MEMBER_ADDED,
        entity_type="membership",
        entity_id=membership.id,
        after={"user": user.email, "role": str(role)},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    return membership


def change_member_role(membership: Membership, role: Role, *, actor: User | None = None) -> None:
    """Change a role, refusing to remove the last owner.

    An organization with no owner cannot manage its own members or billing, and
    recovering from that needs database access. Refuse instead.
    """
    previous = Role(membership.role)
    if previous == role:
        return

    if previous == Role.OWNER and _owner_count(membership.organization_id) <= 1:
        raise ConflictError("This is the only owner. Promote another member to owner first.")

    membership.role = role
    db.session.flush()

    audit.record(
        membership.organization_id,
        audit.MEMBER_ROLE_CHANGED,
        entity_type="membership",
        entity_id=membership.id,
        before={"role": str(previous)},
        after={"role": str(role)},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )


def remove_member(membership: Membership, *, actor: User | None = None) -> None:
    if Role(membership.role) == Role.OWNER and _owner_count(membership.organization_id) <= 1:
        raise ConflictError("This is the only owner and cannot be removed.")

    organization_id = membership.organization_id
    audit.record(
        organization_id,
        audit.MEMBER_REMOVED,
        entity_type="membership",
        entity_id=membership.id,
        before={"user_id": membership.user_id, "role": str(membership.role)},
        actor_id=actor.id if actor else None,
        actor_label=actor.email if actor else "",
    )
    db.session.delete(membership)
    db.session.flush()


def _owner_count(organization_id: str) -> int:
    return (
        db.session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.organization_id == organization_id,
                Membership.role == Role.OWNER,
            )
        )
        or 0
    )


def memberships_for(user: User) -> list[Membership]:
    return list(
        db.session.scalars(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)
        )
    )


# ── Sign-in ─────────────────────────────────────────────────────────────────


def sign_in_blocker(user: User) -> SignInOutcome | None:
    """Why this account may not start a session, or ``None`` if it may.

    Every authentication path must consult this, not just the password one.
    It lives here rather than inside :func:`attempt_sign_in` because the
    massing.cloud handoff re-implemented user resolution without it and
    silently let locked accounts through -- a policy encoded in one caller is a
    policy the next caller forgets.
    """
    if user.is_locked:
        return SignInOutcome.LOCKED
    if not user.is_active:
        return SignInOutcome.INACTIVE
    return None


def attempt_sign_in(email: str, password: str) -> SignInResult:
    """Verify a password and report what should happen next.

    Does not log the user in -- the view does that, after MFA if required.
    """
    user = get_user_by_email(email)

    if user is None:
        # Spend comparable time so response timing does not reveal whether the
        # address is registered.
        verify_password(_timing_equaliser(), password)
        return SignInResult(SignInOutcome.BAD_CREDENTIALS)

    if user.is_locked:
        return SignInResult(SignInOutcome.LOCKED, user)

    if not verify_password(user.password_hash, password):
        _register_failure(user)
        return SignInResult(SignInOutcome.BAD_CREDENTIALS, user)

    # Checked after the password so an attacker cannot probe which accounts
    # are disabled without knowing the password. `sign_in_blocker` is the
    # shared statement of the same policy; this ordering is why it is called
    # here rather than at the top.
    blocked = sign_in_blocker(user)
    if blocked is not None:
        return SignInResult(blocked, user)

    # Correct password: clear the counter whatever happens with MFA next.
    user.failed_login_count = 0
    user.locked_until = None

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    db.session.flush()

    if user.mfa_enabled:
        return SignInResult(SignInOutcome.NEEDS_MFA, user)
    return SignInResult(SignInOutcome.OK, user)


def complete_sign_in(user: User) -> None:
    user.last_login_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    db.session.flush()


def _register_failure(user: User) -> None:
    user.failed_login_count += 1
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        user.locked_until = utcnow() + LOCKOUT_DURATION
        user.failed_login_count = 0
    db.session.flush()


@lru_cache(maxsize=1)
def _timing_equaliser() -> str:
    """A real argon2 hash that no password matches.

    Verified against when the address is unknown so the response takes about as
    long either way. Computed on first use rather than at import so a CLI
    invocation does not pay for an argon2 hash it will never need.
    """
    return hash_password("massingbill-timing-equaliser")
