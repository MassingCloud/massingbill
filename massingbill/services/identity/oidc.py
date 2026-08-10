"""Generic OpenID Connect sign-in.

An **optional adapter**. The core never imports it; CI deletes this file and
re-runs the suite (SPEC.md 3, 13). A standalone install has local password
accounts and needs nothing here.

Deliberately generic. massing.cloud, Microsoft Entra, Google Workspace, Okta
and Procore are all *configuration* rather than special cases -- so the
eventual massing SSO integration is a provider entry, not a code path
(SPEC.md 3.2).

Authorization Code with PKCE, always, including for confidential clients. It
costs one hash and removes a whole class of interception attack, and there is
no configuration in which having it is worse.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from massingbill.services.identity.base import IdentityClaim, IdentityProvider

#: Discovery and JWKS are fetched once per provider instance. The provider is
#: built per request, so this is not a long-lived cache -- deliberately: a
#: rotated signing key should take effect on the next sign-in, not after a
#: redeploy.
DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

REQUEST_TIMEOUT = 10.0


@dataclass(frozen=True)
class AuthorizationRequest:
    """What the caller must keep until the user comes back."""

    url: str
    state: str
    nonce: str
    code_verifier: str


class OidcProvider(IdentityProvider):
    interactive_redirect = True

    def __init__(
        self,
        *,
        name: str,
        issuer: str,
        client_id: str,
        client_secret: str = "",
        redirect_uri: str,
        scopes: str = "openid email profile",
    ) -> None:
        if not issuer or not client_id or not redirect_uri:
            raise ValueError(
                f"The {name!r} OIDC provider needs an issuer, a client id and a redirect URI."
            )
        self.name = name
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._metadata: dict[str, Any] | None = None

    # ── Discovery ───────────────────────────────────────────────────────────

    @property
    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            response = requests.get(f"{self.issuer}{DISCOVERY_SUFFIX}", timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            self._metadata = dict(response.json())
        return self._metadata

    # ── Step one: send them away ────────────────────────────────────────────

    def begin(self) -> AuthorizationRequest:
        """Build the authorization URL, with PKCE and a nonce.

        ``state`` defends the redirect against CSRF; ``nonce`` binds the ID
        token to *this* request so a token replayed from another session does
        not authenticate. They are different problems and need separate values.
        """
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return AuthorizationRequest(
            url=f"{self.metadata['authorization_endpoint']}?{query}",
            state=state,
            nonce=nonce,
            code_verifier=verifier,
        )

    # ── Step two: verify what came back ─────────────────────────────────────

    def authenticate(self, **credentials: object) -> IdentityClaim | None:
        """Exchange the code and verify the ID token.

        Returns ``None`` for every failure rather than raising, matching
        ``LocalPasswordProvider``: the caller answers all of them with one
        message, because distinguishing "unknown user" from "bad token" is a
        useful signal to the wrong person.
        """
        code = str(credentials.get("code", ""))
        verifier = str(credentials.get("code_verifier", ""))
        nonce = str(credentials.get("nonce", ""))

        if not code or not verifier:
            return None

        try:
            tokens = self._exchange(code, verifier)
            claims = self._verify_id_token(tokens["id_token"], nonce)
        except (requests.RequestException, JoseError, KeyError, ValueError):
            return None

        email = str(claims.get("email", ""))
        if not email:
            # An identity with no email cannot be matched to a member, and
            # inventing one from the subject would silently create accounts.
            return None

        return IdentityClaim(
            subject=str(claims["sub"]),
            email=email,
            name=str(claims.get("name", "")),
            provider=self.name,
            email_verified=bool(claims.get("email_verified", False)),
            avatar_url=str(claims.get("picture", "")),
            raw=dict(claims),
        )

    def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        response = requests.post(
            self.metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": verifier,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return dict(response.json())

    def _verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Verify signature, issuer, audience, expiry and nonce.

        Every one of these is load-bearing. A token checked for signature alone
        is a token from any issuer, for any audience, from any time, for any
        session -- four separate ways to accept somebody else's login.

        The algorithm list is explicit. Accepting whatever the token's own
        header asks for is how ``alg: none`` and HMAC-with-the-public-key
        confusion get in.
        """
        jwks = requests.get(self.metadata["jwks_uri"], timeout=REQUEST_TIMEOUT)
        jwks.raise_for_status()

        token = jwt.decode(
            id_token,
            KeySet.import_key_set(jwks.json()),
            algorithms=["RS256", "ES256"],
        )

        claims_requests = jwt.JWTClaimsRegistry(
            iss={"essential": True, "values": [self.metadata["issuer"]]},
            aud={"essential": True, "values": [self.client_id]},
            exp={"essential": True},
        )
        claims_requests.validate(token.claims)

        if nonce and token.claims.get("nonce") != nonce:
            raise ValueError("The ID token nonce does not match this sign-in attempt.")

        return dict(token.claims)
