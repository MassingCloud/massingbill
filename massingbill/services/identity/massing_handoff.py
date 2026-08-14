"""Verifying a signed handoff from the massing.cloud bridge.

An **optional adapter** (SPEC.md 3, 13). The core never imports it; CI deletes
this file and re-runs the suite. A standalone install has local password
accounts and never sees an assertion.

The other half lives in `plugin/massing-billing/includes/class-handoff.php`. A
WordPress user who is already signed in over there arrives here with a
short-lived HMAC-signed assertion; this checks it and produces an
:class:`IdentityClaim`. Nothing about the session is shared -- Massing Bill
starts its own -- because a WordPress plugin in the request path of a pay
application is the worst place for an outage to land.

Standard library only, deliberately. This is HMAC over JSON, it is the
authentication boundary, and a dependency here would be one more thing between
a contractor and their billing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from massingbill.services.identity.base import IdentityClaim

#: Assertions are minted for a redirect. Anything older is not late, it is
#: replayed.
MAX_AGE_SECONDS = 120

#: Refuse an assertion whose expiry is implausibly far out, so a bridge bug
#: cannot mint a token that outlives the session it was meant for.
MAX_LIFETIME_SECONDS = 600


class HandoffError(Exception):
    """The assertion is not acceptable. The reason is never shown to the user."""


@dataclass(frozen=True)
class Handoff:
    """A verified assertion."""

    claim: IdentityClaim
    organization_id: str
    jti: str


def verify(assertion: str, *, secret: str, now: float | None = None) -> Handoff:
    """Check an assertion and return what it asserts.

    Raises :class:`HandoffError` for every failure, with a reason that is for
    the log rather than the browser: telling an attacker *which* check failed
    is free help.

    The caller is responsible for rejecting a ``jti`` it has already seen. That
    is a storage concern and this module deliberately has no storage.
    """
    if not secret:
        raise HandoffError("no shared secret is configured")

    payload, _, signature = assertion.partition(".")
    if not payload or not signature:
        raise HandoffError("malformed assertion")

    # `compare_digest` raises TypeError rather than returning False when a str
    # argument is not ASCII, and the signature half is attacker-controlled. The
    # module's contract is that every failure is a HandoffError, so the check
    # happens here rather than surfacing a TypeError to the caller.
    if not signature.isascii():
        raise HandoffError("signature contains non-ASCII characters")

    expected = _b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())

    # Constant time. A comparison that returns early leaks the signature one
    # byte at a time to anyone willing to measure.
    if not hmac.compare_digest(expected, signature):
        raise HandoffError("bad signature")

    try:
        claims: dict[str, Any] = json.loads(_b64decode(payload))
    except (ValueError, binascii.Error) as exc:
        raise HandoffError("undecodable payload") from exc

    _check_timing(claims, now if now is not None else time.time())

    email = str(claims.get("email", "")).strip()
    organization = str(claims.get("org", "")).strip()
    subject = str(claims.get("sub", "")).strip()

    if not email or not organization or not subject:
        # An assertion missing any of these cannot be matched to a member, and
        # inventing the missing part would silently create an account.
        raise HandoffError("incomplete assertion")

    return Handoff(
        claim=IdentityClaim(
            subject=subject,
            email=email,
            name=str(claims.get("name", "")),
            provider="massing",
            # massing.cloud verifies addresses at registration; a handoff is a
            # statement by an authenticated party, not a self-assertion.
            email_verified=True,
            raw=dict(claims),
        ),
        organization_id=organization,
        jti=str(claims.get("jti", "")),
    )


def _check_timing(claims: dict[str, Any], now: float) -> None:
    """Reject expired, not-yet-valid and over-long assertions."""
    try:
        issued = int(claims.get("iat", 0))
        expires = int(claims.get("exp", 0))
    except (TypeError, ValueError) as exc:
        raise HandoffError("unreadable timestamps") from exc

    if not issued or not expires:
        raise HandoffError("undated assertion")

    if expires - issued > MAX_LIFETIME_SECONDS:
        raise HandoffError("lifetime is longer than a redirect needs")

    # A little tolerance for clock skew between two servers, in both
    # directions. Without it, a machine a few seconds fast refuses everything.
    if now > expires + 30:
        raise HandoffError("expired")

    if issued - now > 30:
        raise HandoffError("issued in the future")

    if now - issued > MAX_AGE_SECONDS:
        raise HandoffError("older than a redirect")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
