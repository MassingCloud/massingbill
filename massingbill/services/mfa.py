"""TOTP two-factor enrolment and verification.

The enrolment QR is rendered by segno as an **inline SVG**. That is not an
aesthetic choice: the content security policy is ``default-src 'self'`` with no
external host and no inline script, so a chart API or a CDN image would be
blocked. Rendering the SVG in-process also means the shared secret never leaves
the machine and is never written to disk.
"""

from __future__ import annotations

import base64
import io

import pyotp
import segno
from flask import current_app

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import User
from massingbill.models.base import utcnow
from massingbill.services import audit
from massingbill.services.crypto import SecretBox

ISSUER = "Massing Bill"

#: How far either side of now a code is accepted, in 30-second steps. One step
#: tolerates ordinary clock drift; more starts to matter for replay.
VALID_WINDOW = 1


def _box() -> SecretBox:
    return SecretBox(current_app.config["MASSINGBILL_SETTINGS"].encryption_key)


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(user: User, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """The enrolment QR as an inline SVG fragment, CSP-safe."""
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer, kind="svg", scale=4, border=2, xmldecl=False, svgns=True
    )
    return buffer.getvalue().decode("utf-8")


def qr_data_uri(uri: str) -> str:
    """The enrolment QR as a ``data:`` URI, for an ``<img>`` tag.

    Permitted because the policy allows ``img-src 'self' data:`` -- no external
    host is contacted.
    """
    svg = qr_svg(uri)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def verify_code(secret: str, code: str) -> bool:
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=VALID_WINDOW))


def begin_enrolment(user: User) -> tuple[str, str]:
    """Return a fresh secret and its provisioning URI. Nothing is stored yet.

    The secret is only persisted once the user proves they can generate a code
    from it -- otherwise a mis-scanned QR locks them out of their own account.
    """
    secret = generate_secret()
    return secret, provisioning_uri(user, secret)


def confirm_enrolment(user: User, secret: str, code: str) -> None:
    if not verify_code(secret, code):
        raise ValidationError("That code is not valid. Check your authenticator and try again.")

    user.totp_secret = _box().encrypt(secret)
    user.totp_confirmed_at = utcnow()
    db.session.flush()


def secret_for(user: User) -> str:
    if user.totp_secret is None:
        raise ValidationError("Two-factor authentication is not enabled for this account.")
    return _box().decrypt(user.totp_secret)


def verify_user_code(user: User, code: str) -> bool:
    if not user.mfa_enabled:
        return False
    return verify_code(secret_for(user), code)


def disable(user: User, organization_id: str | None = None) -> None:
    user.totp_secret = None
    user.totp_confirmed_at = None
    db.session.flush()
    if organization_id:
        audit.record_for_current_user(
            organization_id,
            audit.MFA_DISABLED,
            entity_type="user",
            entity_id=user.id,
        )
