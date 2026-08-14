"""Verifying the signed handoff from the massing.cloud bridge.

This is an authentication boundary, so the negative cases are the point. Each
one is a way somebody could arrive as a user they are not, and each has its own
test rather than being folded into a "rejects bad input" sweep — when one of
these regresses, the failure should name which door opened.

The signatures here are built the way `class-handoff.php` builds them, so the
two sides are checked against each other rather than against my own helper.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import pytest

pytestmark = pytest.mark.adapter

handoff = pytest.importorskip(
    "massingbill.services.identity.massing_handoff",
    reason="the massing handoff adapter is not installed",
)

SECRET = "a-shared-secret-of-reasonable-length"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(secret: str = SECRET, **overrides: Any) -> str:
    """An assertion, built exactly as the PHP bridge builds one."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "42",
        "email": "pm@acme.example",
        "name": "Pat M",
        "org": "org-abc",
        "iat": now,
        "exp": now + 60,
        "jti": "11111111-2222-3333-4444-555555555555",
    }
    claims.update(overrides)

    payload = b64(json.dumps(claims).encode())
    signature = b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


# ── The happy path ──────────────────────────────────────────────────────────


def test_a_well_formed_assertion_verifies() -> None:
    result = handoff.verify(mint(), secret=SECRET)

    assert result.claim.email == "pm@acme.example"
    assert result.claim.subject == "42"
    assert result.claim.provider == "massing"
    assert result.organization_id == "org-abc"
    assert result.jti


def test_the_identity_is_treated_as_verified() -> None:
    """massing.cloud verifies addresses at registration, so a handoff is a
    statement by an authenticated party rather than a self-assertion."""
    assert handoff.verify(mint(), secret=SECRET).claim.email_verified is True


# ── Every way in that must stay shut ────────────────────────────────────────


def test_a_forged_signature_is_refused() -> None:
    with pytest.raises(handoff.HandoffError):
        handoff.verify(mint(secret="not-the-shared-secret"), secret=SECRET)


def test_a_tampered_payload_is_refused() -> None:
    """The classic: keep the signature, change who you are."""
    assertion = mint()
    payload, _, signature = assertion.partition(".")
    claims = json.loads(handoff._b64decode(payload))
    claims["email"] = "attacker@example.com"

    forged = b64(json.dumps(claims).encode())

    with pytest.raises(handoff.HandoffError):
        handoff.verify(f"{forged}.{signature}", secret=SECRET)


def test_an_expired_assertion_is_refused() -> None:
    now = int(time.time())
    with pytest.raises(handoff.HandoffError, match="expired"):
        handoff.verify(mint(iat=now - 300, exp=now - 200), secret=SECRET)


def test_an_assertion_from_the_future_is_refused() -> None:
    """A clock-skew story is also a way to mint something that never expires."""
    now = int(time.time())
    with pytest.raises(handoff.HandoffError, match="future"):
        handoff.verify(mint(iat=now + 600, exp=now + 900), secret=SECRET)


def test_a_long_lived_assertion_is_refused_even_if_unexpired() -> None:
    """A bridge bug that mints a week-long token must not become a week-long
    session. Sixty seconds is what a redirect needs."""
    now = int(time.time())
    with pytest.raises(handoff.HandoffError, match="longer than a redirect"):
        handoff.verify(mint(iat=now, exp=now + 7 * 24 * 3600), secret=SECRET)


def test_an_old_but_unexpired_assertion_is_refused() -> None:
    """Belt and braces against a bridge that sets a generous exp."""
    now = int(time.time())
    with pytest.raises(handoff.HandoffError, match="older than a redirect"):
        handoff.verify(mint(iat=now - 200, exp=now + 300), secret=SECRET)


def test_an_undated_assertion_is_refused() -> None:
    with pytest.raises(handoff.HandoffError, match="undated"):
        handoff.verify(mint(iat=0, exp=0), secret=SECRET)


@pytest.mark.parametrize("missing", ["email", "org", "sub"])
def test_an_incomplete_assertion_is_refused(missing: str) -> None:
    """Each of these is required to match a member. Synthesising any of them
    would silently create an account nobody invited."""
    with pytest.raises(handoff.HandoffError, match="incomplete"):
        handoff.verify(mint(**{missing: ""}), secret=SECRET)


def test_a_malformed_assertion_is_refused() -> None:
    for bad in ("", "nodot", ".", "a.", ".b"):
        with pytest.raises(handoff.HandoffError):
            handoff.verify(bad, secret=SECRET)


def test_undecodable_payload_is_refused() -> None:
    payload = b64(b"this is not json")
    signature = b64(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())

    with pytest.raises(handoff.HandoffError, match="undecodable"):
        handoff.verify(f"{payload}.{signature}", secret=SECRET)


def test_no_secret_refuses_everything() -> None:
    """A deployment that has not configured the bridge must not accept
    assertions signed with an empty string."""
    with pytest.raises(handoff.HandoffError, match="no shared secret"):
        handoff.verify(mint(secret=""), secret="")


def test_the_failure_reason_never_reaches_the_user() -> None:
    """The reasons are for the log. Telling an attacker which check failed is
    free help, so the caller is expected to answer all of them the same way --
    this test exists to keep that intent visible."""
    reasons = set()
    now = int(time.time())

    for assertion in (mint(secret="wrong"), mint(iat=now - 300, exp=now - 200), "junk"):
        try:
            handoff.verify(assertion, secret=SECRET)
        except handoff.HandoffError as exc:
            reasons.add(str(exc))

    assert len(reasons) == 3, "distinct reasons are logged"


# ── Replay is the caller's job, and the module says so ──────────────────────


def test_the_same_assertion_verifies_twice() -> None:
    """Deliberate. This module has no storage, so it cannot enforce single use;
    the jti is returned precisely so the caller can. Pinned so nobody assumes
    replay protection lives here."""
    assertion = mint()

    first = handoff.verify(assertion, secret=SECRET)
    second = handoff.verify(assertion, secret=SECRET)

    assert first.jti == second.jti


# ── The two languages actually agree ────────────────────────────────────────

#: A name with an em dash and two diacritics. Written as escapes so this file's
#: own encoding cannot be what makes the test pass.
UNICODE_NAME = "Pat M \u2014 \u00dcn\u00efcode"

#: The signing half of `class-handoff.php`, verbatim enough to be a real check
#: of the contract rather than of a re-implementation.
PHP_MINTER = """<?php
function b64(string $value): string {
    return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
}
$secret = getenv('MB_SECRET');
$claims = array(
    'sub'   => '42',
    'email' => 'pm@acme.example',
    'name'  => "Pat M \\u{2014} \\u{00dc}n\\u{00ef}code",
    'org'   => 'org-abc',
    'iat'   => time(),
    'exp'   => time() + 60,
    'jti'   => 'php-minted',
);
$payload = b64(json_encode($claims));
echo $payload . '.' . b64(hash_hmac('sha256', $payload, $secret, true));
"""


def test_an_assertion_minted_by_php_verifies_here(tmp_path) -> None:
    """The tests above mint assertions the way I believe the PHP bridge does.
    This one makes PHP do it, because base64 padding and JSON escaping are
    exactly where two languages quietly disagree, and the failure would be an
    unexplainable sign-in loop in production.

    The program is written to a UTF-8 file rather than passed with ``php -r``.
    A command line carries whatever the shell's code page happens to be, which
    mangled the non-ASCII name before PHP ever parsed it -- the encoding bug
    this test exists to catch, arriving one layer too early to be the one under
    test.

    Skipped when php is absent.
    """
    import os
    import shutil
    import subprocess

    php = shutil.which("php")
    if not php:
        pytest.skip("php is not installed")

    script = tmp_path / "mint.php"
    script.write_text(PHP_MINTER, encoding="utf-8")

    environment = dict(os.environ)
    environment["MB_SECRET"] = SECRET

    result = subprocess.run(  # noqa: S603 - our own file, resolved interpreter
        [php, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=True,
    )

    assertion = result.stdout.strip()

    # Verified against the assertion's own clock, not the wall clock. This test
    # is about base64, JSON escaping and HMAC agreeing across two languages;
    # expiry is covered by six dedicated tests above. Left on the wall clock it
    # measured how long a PHP interpreter takes to start, and a `php.BAT` shim
    # that holds the pipe open aged a 60-second assertion out before it could
    # be checked -- a flake that says nothing about the contract.
    import json as _json

    issued = _json.loads(handoff._b64decode(assertion.split(".")[0]))["iat"]
    verified = handoff.verify(assertion, secret=SECRET, now=issued)

    assert verified.claim.email == "pm@acme.example"
    assert verified.organization_id == "org-abc"
    assert verified.jti == "php-minted"

    # Non-ASCII survives byte for byte. PHP escapes it as \uXXXX on the way
    # out; a decode that guessed latin-1 on this side would go unnoticed until
    # a contractor called Jos\u00e9 could not sign in.
    assert verified.claim.name == UNICODE_NAME


def test_a_non_ascii_signature_is_a_handoff_error_not_a_typeerror() -> None:
    """`hmac.compare_digest` raises TypeError rather than returning False when a
    str argument is not ASCII, and the signature half is attacker-controlled.
    The module's contract is that every failure is a HandoffError."""
    with pytest.raises(handoff.HandoffError, match="non-ASCII"):
        handoff.verify("cGF5bG9hZA.sïgnature", secret=SECRET)
