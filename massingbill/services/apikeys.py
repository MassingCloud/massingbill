"""Minting and verifying organization-scoped API keys.

Token shape, which is a contract with every client that ever stores one::

    mbil_<public_id>_<secret>
         └ 24 hex     └ 43 urlsafe base64 (256 bits)

The prefix is what makes a leaked key findable. ``mbil_`` is greppable in a log
aggregator and matchable by secret-scanning tools, and the ``public_id`` lets an
operator name the exact key to revoke from a log line that never contained the
secret.

Verification is: split, look the row up by ``public_id``, then compare a SHA-256
digest of the presented secret in constant time. The lookup never touches the
secret, and the comparison never short-circuits.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import ApiKey, Organization, User, utcnow
from massingbill.services import audit
from massingbill.services.rbac import ALL_PERMISSIONS, READ_PERMISSIONS, ApiPrincipal

PREFIX = "mbil"

#: Long enough that collisions are not a thing we think about, short enough to
#: paste into a support ticket.
PUBLIC_ID_BYTES = 12
SECRET_BYTES = 32

#: ``last_used_at`` is observability, not accounting. Writing it on every
#: request would put a write in the path of every read, so it is coarsened --
#: an API key's last use is interesting to the minute, never to the millisecond.
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True)
class MintedKey:
    """A new key and its one and only appearance in plaintext."""

    key: ApiKey
    token: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_scopes(scopes: frozenset[str] | set[str] | list[str] | None) -> frozenset[str]:
    """Validate a requested scope set, defaulting to read-only.

    Defaulting to read rather than to the caller's full authority: a key minted
    by an owner who did not think about scopes should not be able to submit
    applications.
    """
    if scopes is None:
        return READ_PERMISSIONS

    requested = frozenset(scopes)
    unknown = requested - ALL_PERMISSIONS
    if unknown:
        raise ValidationError(
            "Unknown scope(s): " + ", ".join(sorted(unknown)),
            details={"unknown_scopes": sorted(unknown)},
        )
    return requested


def mint(
    organization: Organization,
    *,
    name: str,
    scopes: frozenset[str] | set[str] | list[str] | None = None,
    expires_at: datetime | None = None,
    rate_limit_per_minute: int = 0,
    actor: User | None = None,
) -> MintedKey:
    """Create a key and return the token exactly once.

    The caller must show the token to the user immediately; there is no second
    chance, because only its digest is kept.
    """
    if not name.strip():
        raise ValidationError("Give the key a name, so it can be recognised later.")

    granted = normalize_scopes(scopes)
    public_id = secrets.token_hex(PUBLIC_ID_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)

    key = ApiKey(
        organization_id=organization.id,
        public_id=public_id,
        secret_hash=_hash(secret),
        name=name.strip(),
        scopes=" ".join(sorted(granted)),
        created_by_user_id=actor.id if actor else None,
        expires_at=expires_at,
        rate_limit_per_minute=max(0, rate_limit_per_minute),
    )
    db.session.add(key)
    db.session.flush()

    audit.record(
        organization.id,
        "apikey.minted",
        actor_id=actor.id if actor else None,
        entity_type="api_key",
        entity_id=key.id,
        # The scopes are the point of the record: "who could do what, from when".
        after={"name": key.name, "scopes": sorted(granted), "public_id": public_id},
    )

    return MintedKey(key=key, token=f"{PREFIX}_{public_id}_{secret}")


def parse(token: str) -> tuple[str, str] | None:
    """Split a presented token, or ``None`` if it is not one of ours.

    ``maxsplit=2`` is load-bearing. ``token_urlsafe`` draws from the base64url
    alphabet, which *includes* the underscore, so an unbounded split tore apart
    roughly half of all issued keys -- and did it at mint time, at random, which
    is the worst possible way to find out.
    """
    parts = token.strip().split("_", 2)
    if len(parts) != 3:
        return None
    prefix, public_id, secret = parts
    if prefix != PREFIX or not public_id or not secret:
        return None
    return public_id, secret


def authenticate(token: str) -> ApiKey | None:
    """The key this token proves possession of, or ``None``.

    Returns ``None`` for every failure -- malformed, unknown, wrong secret,
    revoked, expired. The caller answers all of them with one 401 and one
    message: distinguishing "no such key" from "wrong secret" tells an attacker
    which half of a guess was right.
    """
    parsed = parse(token)
    if parsed is None:
        return None
    public_id, secret = parsed

    key = db.session.scalar(db.select(ApiKey).where(ApiKey.public_id == public_id))
    if key is None:
        # Hash anyway. Returning early on an unknown public_id makes the "no
        # such key" path measurably faster than the "wrong secret" path, which
        # is an oracle for which half of a guessed token was right.
        _hash(secret)
        return None

    if not hmac.compare_digest(key.secret_hash, _hash(secret)):
        return None
    if not key.is_usable:
        return None

    _touch(key)
    return key


def _touch(key: ApiKey) -> None:
    """Record the use, coarsely (see :data:`LAST_USED_RESOLUTION`)."""
    now = utcnow()
    if key.last_used_at is not None and now - key.last_used_at < LAST_USED_RESOLUTION:
        return
    key.last_used_at = now
    key.use_count += 1


def principal_for(key: ApiKey) -> ApiPrincipal:
    return ApiPrincipal(
        organization_id=key.organization_id,
        scopes=key.scope_set,
        key_id=key.id,
    )


def revoke(key: ApiKey, *, actor: User | None = None) -> None:
    """Revoke, never delete. A key that was used needs to stay explicable."""
    if key.revoked_at is not None:
        return
    key.revoked_at = utcnow()
    audit.record(
        key.organization_id,
        "apikey.revoked",
        actor_id=actor.id if actor else None,
        entity_type="api_key",
        entity_id=key.id,
        before={"name": key.name, "public_id": key.public_id},
    )


def for_organization(organization_id: str, *, include_revoked: bool = False) -> list[ApiKey]:
    query = db.select(ApiKey).where(ApiKey.organization_id == organization_id)
    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))
    return list(db.session.scalars(query.order_by(ApiKey.created_at.desc())))
