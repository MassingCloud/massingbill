"""Turning a verified massing.cloud assertion into a session.

The cryptography lives in the optional adapter
``services/identity/massing_handoff``; this is the part that has a database and
therefore the part that can enforce single use and decide who the assertion
refers to.

Two decisions worth stating, because both are the conservative end of a real
choice:

**No account is ever created.** A valid assertion says "massing.cloud believes
this person is entitled", not "give this person a login". If the email does not
already belong to a member of the asserted organization, the sign-in is refused.
Anyone holding the shared secret could otherwise mint themselves an account in
any tenant, which turns one leaked secret into total compromise rather than into
the ability to impersonate people who already exist.

**Every refusal reads the same to the browser.** The reasons are logged. Telling
a caller whether the signature failed, the assertion expired, or the account
simply does not exist is free reconnaissance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from massingbill.extensions import db
from massingbill.models import Membership, Organization, SpentHandoff, User
from massingbill.models.base import utcnow
from massingbill.services import accounts, audit

#: What every failure says out loud.
REFUSAL = "That sign-in link is not valid. Sign in here instead."

#: Rows older than this describe assertions that could not be valid under any
#: clock skew, so they are only taking up space.
KEEP_SPENT_FOR = timedelta(hours=1)


class HandoffRejectedError(Exception):
    """The handoff cannot sign anybody in. ``reason`` is for the log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Accepted:
    """Who to sign in, and where."""

    user: User
    organization: Organization


def accept(assertion: str, *, secret: str) -> Accepted:
    """Verify, spend, and resolve an assertion to a member.

    Raises :class:`HandoffRejectedError` for every failure.
    """
    from massingbill.services.identity import massing_handoff

    try:
        verified = massing_handoff.verify(assertion, secret=secret)
    except massing_handoff.HandoffError as exc:
        raise HandoffRejectedError(f"assertion rejected: {exc}") from exc

    _spend(verified.jti)

    organization = db.session.get(Organization, verified.organization_id)
    if organization is None:
        raise HandoffRejectedError(f"unknown organization {verified.organization_id!r}")

    user = accounts.get_user_by_email(verified.claim.email)
    if user is None:
        # Deliberately not created. See the module docstring.
        raise HandoffRejectedError(f"no account for {verified.claim.email!r}")

    membership = db.session.scalar(
        db.select(Membership).where(
            Membership.organization_id == organization.id,
            Membership.user_id == user.id,
        )
    )
    if membership is None:
        raise HandoffRejectedError(
            f"{verified.claim.email!r} is not a member of {organization.id!r}"
        )

    audit.record(
        organization.id,
        "auth.handoff_accepted",
        entity_type="user",
        entity_id=user.id,
        after={"provider": "massing", "jti": verified.jti},
        actor_id=user.id,
        actor_label=user.email,
    )

    return Accepted(user=user, organization=organization)


def _spend(jti: str) -> None:
    """Record the assertion as used, or refuse it as already used.

    The insert *is* the check. Reading first and inserting second would let two
    simultaneous presentations of the same assertion both pass, which is exactly
    the race a replayed URL creates.
    """
    if not jti:
        raise HandoffRejectedError("assertion carries no jti, so single use cannot be enforced")

    db.session.add(SpentHandoff(jti=jti))
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise HandoffRejectedError(f"assertion {jti!r} has already been used") from exc


def prune(*, now: object = None) -> int:
    """Delete spent records that can no longer matter. Returns how many."""
    cutoff = (now or utcnow()) - KEEP_SPENT_FOR  # type: ignore[operator]

    deleted = db.session.query(SpentHandoff).filter(SpentHandoff.used_at < cutoff).delete()
    db.session.flush()
    return int(deleted)
