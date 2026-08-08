"""Signed tokens, password rehashing, and the money/basis-point column helpers."""

from __future__ import annotations

from datetime import datetime

import pytest
from flask import Flask
from freezegun import freeze_time
from sqlalchemy import BigInteger, Integer

from massingbill.errors import AdapterUnavailableError
from massingbill.models.base import bp_column, money_column, new_uuid, utcnow
from massingbill.security import hash_password, load_timed_token, make_serializer, needs_rehash
from massingbill.services.identity import get_provider as get_identity_provider
from massingbill.services.storage import get_backend

# ── Signed tokens ───────────────────────────────────────────────────────────


def test_timed_token_round_trip(app: Flask) -> None:
    token = make_serializer(app, "invite").dumps({"org": "acme", "role": "pm"})
    assert load_timed_token(app, "invite", token, max_age=60) == {"org": "acme", "role": "pm"}


def test_expired_token_returns_none(app: Flask) -> None:
    # Frozen clock rather than a real sleep: itsdangerous stamps whole seconds,
    # so a sleep-based test is both slow and flaky at the boundary.
    with freeze_time("2026-01-01 00:00:00"):
        token = make_serializer(app, "invite").dumps({"org": "acme"})
    with freeze_time("2026-01-01 00:05:00"):
        assert load_timed_token(app, "invite", token, max_age=60) is None


def test_a_token_inside_its_window_still_loads(app: Flask) -> None:
    with freeze_time("2026-01-01 00:00:00"):
        token = make_serializer(app, "invite").dumps({"org": "acme"})
    with freeze_time("2026-01-01 00:00:30"):
        assert load_timed_token(app, "invite", token, max_age=60) == {"org": "acme"}


def test_tampered_token_returns_none(app: Flask) -> None:
    token = make_serializer(app, "invite").dumps({"org": "acme"})
    assert load_timed_token(app, "invite", token + "x", max_age=60) is None


def test_a_token_minted_for_one_purpose_is_useless_for_another(app: Flask) -> None:
    """Salt separation: a share link must not double as an invitation."""
    token = make_serializer(app, "share").dumps({"application": "app-1"})
    assert load_timed_token(app, "invite", token, max_age=60) is None


def test_needs_rehash_flags_a_corrupt_hash() -> None:
    assert needs_rehash("not-a-hash") is True


def test_needs_rehash_accepts_a_current_hash() -> None:
    assert needs_rehash(hash_password("passphrase")) is False


# ── Column helpers ──────────────────────────────────────────────────────────


def test_money_columns_are_bigint_not_float() -> None:
    """SPEC.md 5: the single most important schema decision in the product."""
    column = money_column()
    assert isinstance(column.column.type, BigInteger)


def test_money_columns_default_to_zero_cents() -> None:
    assert money_column().column.default.arg == 0


def test_money_columns_can_be_nullable() -> None:
    assert money_column(nullable=True, default=None).column.nullable is True


def test_basis_point_columns_are_integers() -> None:
    assert isinstance(bp_column().column.type, Integer)


# ── Small helpers ───────────────────────────────────────────────────────────


def test_new_uuid_is_hex_and_unique() -> None:
    first, second = new_uuid(), new_uuid()
    assert len(first) == 32
    assert int(first, 16) >= 0
    assert first != second


def test_utcnow_is_timezone_aware() -> None:
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None


# ── Adapter absence ─────────────────────────────────────────────────────────


def test_requesting_oidc_before_p7_explains_itself() -> None:
    with pytest.raises(AdapterUnavailableError, match="pip install"):
        get_identity_provider("google")


def test_requesting_s3_before_p7_explains_itself() -> None:
    with pytest.raises(AdapterUnavailableError, match="pip install"):
        get_backend("s3", bucket="x")


def test_requesting_the_vault_before_p9_explains_itself() -> None:
    with pytest.raises(AdapterUnavailableError, match="pip install"):
        get_backend("massing_vault")
