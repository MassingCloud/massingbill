"""Local password identity provider tests."""

from __future__ import annotations

from massingbill.security import hash_password
from massingbill.services.identity import LocalCredential, LocalPasswordProvider


def _provider(*records: LocalCredential) -> LocalPasswordProvider:
    by_email = {record.email: record for record in records}
    return LocalPasswordProvider(lookup=by_email.get)


def test_successful_authentication_returns_a_claim() -> None:
    provider = _provider(
        LocalCredential(
            subject="user-1",
            email="pm@example.com",
            password_hash=hash_password("s3cret-passphrase"),
            name="Dana Reyes",
        )
    )

    claim = provider.authenticate(email="pm@example.com", password="s3cret-passphrase")

    assert claim is not None
    assert claim.subject == "user-1"
    assert claim.provider == "local"
    assert claim.email_verified is True


def test_email_is_matched_case_insensitively() -> None:
    provider = _provider(
        LocalCredential(
            subject="user-1", email="pm@example.com", password_hash=hash_password("pw-correct")
        )
    )
    assert provider.authenticate(email="  PM@Example.com ", password="pw-correct") is not None


def test_wrong_password_is_rejected() -> None:
    provider = _provider(
        LocalCredential(
            subject="user-1", email="pm@example.com", password_hash=hash_password("pw-correct")
        )
    )
    assert provider.authenticate(email="pm@example.com", password="pw-wrong") is None


def test_unknown_account_is_rejected() -> None:
    assert _provider().authenticate(email="nobody@example.com", password="whatever") is None


def test_inactive_account_is_rejected() -> None:
    provider = _provider(
        LocalCredential(
            subject="user-1",
            email="pm@example.com",
            password_hash=hash_password("pw-correct"),
            is_active=False,
        )
    )
    assert provider.authenticate(email="pm@example.com", password="pw-correct") is None


def test_missing_credentials_are_rejected() -> None:
    provider = _provider()
    assert provider.authenticate() is None
    assert provider.authenticate(email="pm@example.com") is None
    assert provider.authenticate(password="pw") is None
