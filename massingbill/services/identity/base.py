"""The identity seam.

``LocalPasswordProvider`` (argon2id + TOTP) is the default and the only provider
a standalone install needs. ``OidcProvider`` is generic -- massing.cloud,
Microsoft, Google, Procore, Okta and any other OIDC issuer are configuration,
not special cases (SPEC.md 3.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdentityClaim:
    """A verified identity, normalised across providers."""

    subject: str
    email: str
    name: str = ""
    provider: str = "local"
    email_verified: bool = False
    avatar_url: str = ""
    raw: dict[str, object] = field(default_factory=dict)


class IdentityProvider(ABC):
    name = "base"

    #: True when sign-in leaves the application (an OIDC redirect), which the
    #: login view uses to decide between rendering a form and issuing a 302.
    interactive_redirect = False

    @abstractmethod
    def authenticate(self, **credentials: object) -> IdentityClaim | None:
        """Return a verified claim, or ``None`` when authentication fails."""
