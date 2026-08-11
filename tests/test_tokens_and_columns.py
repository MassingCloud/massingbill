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


#: What "asked for an adapter and did not get one" looks like, in either world.
#:
#: These two used to assert "OIDC and S3 are not built yet". They are built now,
#: so they assert the thing that is still true: naming an adapter is not the
#: same as configuring one, and it must fail at startup rather than at
#: somebody's first sign-in or first upload.
#:
#: Both outcomes have to be accepted because the suite genuinely runs in both
#: worlds -- the no-adapters CI job deletes these modules and re-runs
#: everything, and there the same call correctly raises AdapterUnavailableError
#: instead. Pinning only one of them makes this test a report on which CI job
#: is running.
NOT_CONFIGURED = (AdapterUnavailableError, ValueError, TypeError)


def test_an_oidc_provider_must_be_configured_not_merely_named() -> None:
    with pytest.raises(NOT_CONFIGURED):
        get_identity_provider("google")


def test_s3_storage_must_be_configured_not_merely_selected() -> None:
    with pytest.raises(NOT_CONFIGURED):
        get_backend("s3", bucket="")


def test_the_vault_must_be_configured_not_merely_selected() -> None:
    """P9 has shipped, so this asserts the thing that is still true: naming a
    backend is not configuring one, and a half-configured vault must fail at
    startup rather than at the first document somebody tries to store.

    The genuinely-not-installed path is covered in ``test_adapters.py``, which
    forces the ImportError rather than relying on a file being absent.
    """
    with pytest.raises(NOT_CONFIGURED):
        get_backend("massing_vault", api_key="")
