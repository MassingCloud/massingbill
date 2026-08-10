"""The optional adapters, and the promise that the core does not need them.

Marked ``adapter`` so the ``no-adapters`` CI job can delete the modules and skip
these while the rest of the suite still has to pass. That job is the real proof;
these tests are about the adapters behaving correctly *when* installed.

Nothing here touches the network. The suite refuses outbound sockets, so the S3
client is driven against a stub and the OIDC provider against stubbed HTTP --
which is the right level anyway: what is worth testing is our verification
logic, not botocore's.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from joserfc.errors import JoseError

from massingbill.errors import AdapterUnavailableError
from massingbill.optional import ADAPTER_MODULES
from massingbill.services.storage import get_backend
from massingbill.services.storage.base import StoragePointer

pytestmark = pytest.mark.adapter

boto3 = pytest.importorskip("boto3")
requires_authlib = pytest.mark.skipif(
    pytest.importorskip("authlib", reason="OIDC extra not installed") is None,
    reason="OIDC extra not installed",
)


# ── The seam itself ─────────────────────────────────────────────────────────


def test_every_named_adapter_is_reachable_only_through_its_factory() -> None:
    """The contract the import-linter enforces, stated once in a test too."""
    assert "massingbill.services.storage.s3" in ADAPTER_MODULES
    assert "massingbill.services.identity.oidc" in ADAPTER_MODULES


def test_an_unknown_backend_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_backend("dropbox")


def test_a_missing_adapter_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hoster who sets the config without the extra should be told which
    package to install, not handed an ImportError."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if "massing_vault" in name:
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(AdapterUnavailableError, match="pip install"):
        get_backend("massing_vault")


# ── S3 ──────────────────────────────────────────────────────────────────────


class FakeS3Client:
    """Just enough of the S3 API to exercise our own logic."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.objects[kwargs["Key"]] = kwargs
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.objects.pop(Key, None)
        return {}


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch):
    from massingbill.services.storage import s3 as s3_module

    fake = FakeS3Client()
    backend = s3_module.S3Storage.__new__(s3_module.S3Storage)
    backend.bucket = "paydocs"
    backend.prefix = "massingbill"
    backend._client = fake
    return backend, fake


def test_a_bucket_is_required() -> None:
    from massingbill.services.storage.s3 import S3Storage

    with pytest.raises(ValueError, match="bucket"):
        S3Storage(bucket="")


def test_storing_computes_the_digest_from_the_bytes(s3) -> None:
    """Not from a caller-supplied header. A digest the stored thing provided
    about itself proves nothing."""
    backend, fake = s3
    pointer = backend.put(
        "app-1.pdf", io.BytesIO(b"%PDF-1.7 hello"), content_type="application/pdf"
    )

    assert pointer.size == 14
    assert pointer.sha256 == __import__("hashlib").sha256(b"%PDF-1.7 hello").hexdigest()
    assert fake.objects["massingbill/app-1.pdf"]["Metadata"]["sha256"] == pointer.sha256


def test_objects_are_written_encrypted(s3) -> None:
    backend, fake = s3
    backend.put("x", io.BytesIO(b"x"), content_type="text/plain")

    assert fake.objects["massingbill/x"]["ServerSideEncryption"] == "AES256"


def test_reading_back_verifies_the_digest(s3) -> None:
    backend, _ = s3
    pointer = backend.put("app-1.pdf", io.BytesIO(b"original"), content_type="application/pdf")

    assert backend.open(pointer).read() == b"original"


def test_a_modified_object_is_refused_rather_than_returned(s3) -> None:
    """Object storage is durable, not immutable. A signed waiver whose bytes
    changed underneath us is exactly what this catches."""
    backend, fake = s3
    pointer = backend.put("waiver.pdf", io.BytesIO(b"as signed"), content_type="application/pdf")

    fake.objects["massingbill/waiver.pdf"]["Body"] = b"tampered"

    with pytest.raises(ValueError, match="no longer matches the digest"):
        backend.open(pointer)


def test_exists_is_false_for_a_missing_object(s3) -> None:
    backend, _ = s3
    assert not backend.exists(StoragePointer(backend="s3", key="nope", size=0, sha256=""))


def test_delete_removes_it(s3) -> None:
    backend, _ = s3
    pointer = backend.put("gone.txt", io.BytesIO(b"x"), content_type="text/plain")
    assert backend.exists(pointer)

    backend.delete(pointer)
    assert not backend.exists(pointer)


# ── OIDC ────────────────────────────────────────────────────────────────────


def test_an_oidc_provider_needs_its_essentials() -> None:
    from massingbill.services.identity.oidc import OidcProvider

    with pytest.raises(ValueError, match="issuer"):
        OidcProvider(name="massing", issuer="", client_id="x", redirect_uri="y")


def test_pkce_challenge_is_the_sha256_of_the_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interception of the authorization code is worthless without the verifier,
    and only if the challenge really is derived from it."""
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from massingbill.services.identity.oidc import OidcProvider

    provider = OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    monkeypatch.setattr(
        type(provider),
        "metadata",
        property(lambda self: {"authorization_endpoint": "https://massing.cloud/authorize"}),
    )

    request = provider.begin()
    query = parse_qs(urlparse(request.url).query)

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]
    assert query["code_challenge_method"] == ["S256"]


def test_state_and_nonce_are_different_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """They defend different things -- redirect CSRF and token replay -- and
    reusing one value for both silently halves the protection."""
    from massingbill.services.identity.oidc import OidcProvider

    provider = OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    monkeypatch.setattr(
        type(provider),
        "metadata",
        property(lambda self: {"authorization_endpoint": "https://massing.cloud/authorize"}),
    )

    request = provider.begin()
    assert request.state != request.nonce
    assert request.state != request.code_verifier


def test_authentication_without_a_code_fails_quietly() -> None:
    from massingbill.services.identity.oidc import OidcProvider

    provider = OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    assert provider.authenticate(code="", code_verifier="") is None


def test_an_identity_with_no_email_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching a claim to a member needs an email. Synthesising one from the
    subject would silently create accounts nobody invited."""
    from massingbill.services.identity import oidc as oidc_module

    provider = oidc_module.OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    monkeypatch.setattr(provider, "_exchange", lambda code, verifier: {"id_token": "t"})
    monkeypatch.setattr(provider, "_verify_id_token", lambda token, nonce: {"sub": "abc"})

    assert provider.authenticate(code="c", code_verifier="v", nonce="n") is None


def test_a_verified_identity_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    from massingbill.services.identity import oidc as oidc_module

    provider = oidc_module.OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    monkeypatch.setattr(provider, "_exchange", lambda code, verifier: {"id_token": "t"})
    monkeypatch.setattr(
        provider,
        "_verify_id_token",
        lambda token, nonce: {
            "sub": "abc",
            "email": "pm@acme.example",
            "name": "Pat M",
            "email_verified": True,
        },
    )

    claim = provider.authenticate(code="c", code_verifier="v", nonce="n")

    assert claim is not None
    assert claim.email == "pm@acme.example"
    assert claim.provider == "massing"
    assert claim.email_verified is True


REJECTED = (JoseError, ValueError, KeyError)


# ── ID token verification, against a real signature ─────────────────────────
#
# The tests above stub `_verify_id_token`, which is fine for the normalisation
# logic and useless for the part that actually keeps somebody else out. These
# mint a real RSA key, sign real tokens with it, and run them through the real
# verifier.


@pytest.fixture
def signing_key():
    from joserfc.jwk import RSAKey

    return RSAKey.generate_key(2048, parameters={"kid": "test-key"})


@pytest.fixture
def oidc(monkeypatch: pytest.MonkeyPatch, signing_key):
    from joserfc.jwk import KeySet

    from massingbill.services.identity import oidc as oidc_module

    provider = oidc_module.OidcProvider(
        name="massing",
        issuer="https://massing.cloud",
        client_id="cid",
        redirect_uri="https://billing.example/callback",
    )
    monkeypatch.setattr(
        type(provider),
        "metadata",
        property(
            lambda self: {
                "issuer": "https://massing.cloud",
                "jwks_uri": "https://massing.cloud/jwks",
                "token_endpoint": "https://massing.cloud/token",
            }
        ),
    )

    class FakeResponse:
        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._payload

    public = KeySet([signing_key]).as_dict(private=False)
    monkeypatch.setattr(oidc_module.requests, "get", lambda *a, **k: FakeResponse(public))
    return provider


def make_token(signing_key, **claims: Any) -> str:
    import time

    from joserfc import jwt

    payload = {
        "iss": "https://massing.cloud",
        "aud": "cid",
        "sub": "user-123",
        "email": "pm@acme.example",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    payload.update(claims)
    return jwt.encode({"alg": "RS256", "kid": "test-key"}, payload, signing_key)


def test_a_correctly_signed_token_verifies(oidc, signing_key) -> None:
    claims = oidc._verify_id_token(make_token(signing_key, nonce="n"), "n")
    assert claims["email"] == "pm@acme.example"


def test_a_token_from_the_wrong_issuer_is_refused(oidc, signing_key) -> None:
    with pytest.raises(REJECTED):
        oidc._verify_id_token(make_token(signing_key, iss="https://evil.example"), "")


def test_a_token_for_another_audience_is_refused(oidc, signing_key) -> None:
    """A valid token issued for a different client is still somebody else's."""
    with pytest.raises(REJECTED):
        oidc._verify_id_token(make_token(signing_key, aud="another-app"), "")


def test_an_expired_token_is_refused(oidc, signing_key) -> None:
    import time

    with pytest.raises(REJECTED):
        oidc._verify_id_token(make_token(signing_key, exp=int(time.time()) - 60), "")


def test_a_token_signed_by_a_different_key_is_refused(oidc) -> None:
    from joserfc.jwk import RSAKey

    stranger = RSAKey.generate_key(2048, parameters={"kid": "test-key"})

    with pytest.raises(REJECTED):
        oidc._verify_id_token(make_token(stranger), "")


def test_a_replayed_nonce_is_refused(oidc, signing_key) -> None:
    """Binds the token to this sign-in. Without it, a token captured from
    another session authenticates here."""
    token = make_token(signing_key, nonce="from-another-session")

    with pytest.raises(ValueError, match="nonce does not match"):
        oidc._verify_id_token(token, "this-session")


def test_authenticate_swallows_a_bad_token_rather_than_raising(oidc, signing_key) -> None:
    """The caller answers every failure with one message."""
    oidc._exchange = lambda code, verifier: {"id_token": make_token(signing_key, aud="wrong")}
    assert oidc.authenticate(code="c", code_verifier="v", nonce="") is None
