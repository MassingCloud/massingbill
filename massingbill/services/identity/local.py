"""Local password accounts -- the default identity provider.

Deliberately thin at P0: it verifies a password against a stored argon2id hash
and returns a normalised claim. User records, TOTP enrolment, invitations and
lockout arrive in P2; this exists now so the seam is real from the first commit
rather than retrofitted around whatever P2 happens to build.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from massingbill.security import verify_password

from .base import IdentityClaim, IdentityProvider


@dataclass(frozen=True)
class LocalCredential:
    """The minimum a lookup must return for authentication to be decidable."""

    subject: str
    email: str
    password_hash: str
    name: str = ""
    is_active: bool = True


#: Injected by the caller so this module never imports the model layer -- which
#: is what keeps the import-linter contract in pyproject.toml satisfiable.
CredentialLookup = Callable[[str], LocalCredential | None]


class LocalPasswordProvider(IdentityProvider):
    name = "local"
    interactive_redirect = False

    def __init__(self, lookup: CredentialLookup) -> None:
        self._lookup = lookup

    def authenticate(self, **credentials: object) -> IdentityClaim | None:
        email = str(credentials.get("email", "")).strip().lower()
        password = str(credentials.get("password", ""))
        if not email or not password:
            return None

        record = self._lookup(email)
        if record is None:
            # Spend comparable time on an unknown address so response timing
            # does not reveal whether the account exists.
            verify_password(
                "$argon2id$v=19$m=65536,t=3,p=2$"
                "c29tZXNhbHRzb21lc2FsdA$0000000000000000000000000000000000000000000",
                password,
            )
            return None

        if not record.is_active or not verify_password(record.password_hash, password):
            return None

        return IdentityClaim(
            subject=record.subject,
            email=record.email,
            name=record.name,
            provider=self.name,
            email_verified=True,
        )
