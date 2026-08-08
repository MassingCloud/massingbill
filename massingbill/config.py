"""Configuration.

Every setting has a default that works with **no environment file and no
network** -- that is the P0 acceptance criterion and the whole premise of
SPEC.md section 3. Optional adapters are selected by name here; if nobody
selects them, their code is never imported.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Selectable adapter implementations (SPEC.md 3.2). The first value of each is
# the standalone default and is the only one the core test suite exercises.
EntitlementBackend = Literal["standalone", "massing_cloud"]
StorageBackend = Literal["local", "s3", "massing_vault"]


class Settings(BaseSettings):
    """Runtime settings, read from ``MASSINGBILL_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MASSINGBILL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Core ────────────────────────────────────────────────────────────────
    env: Literal["development", "testing", "production"] = "development"
    secret_key: str = Field(default="")
    instance_path: Path = Field(default=Path("instance"))
    database_url: str = Field(default="")

    # ── Adapters (SPEC.md 3.2) ──────────────────────────────────────────────
    entitlement_provider: EntitlementBackend = "standalone"
    storage_backend: StorageBackend = "local"
    #: Empty means local password accounts only. Values name OIDC providers
    #: configured under ``oidc_*``; the massing.cloud broker is just one of them.
    oidc_providers: tuple[str, ...] = ()

    # ── Optional: massing.cloud adapter. Unset in a standalone install. ─────
    massing_base_url: str = ""
    massing_shared_secret: str = ""
    massing_license_key: str = ""

    # ── Optional: S3-compatible storage. Unset in a standalone install. ─────
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # ── Web ─────────────────────────────────────────────────────────────────
    session_cookie_secure: bool = True
    rate_limit_default: str = "240 per hour"
    rate_limit_storage_uri: str = "memory://"
    max_content_length: int = 32 * 1024 * 1024  # 32 MB uploads
    preferred_url_scheme: str = "https"

    # ── Observability ───────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("oidc_providers", mode="before")
    @classmethod
    def _split_providers(cls, value: object) -> object:
        """Accept ``MASSINGBILL_OIDC_PROVIDERS=massing,google`` from the env."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _apply_defaults(self) -> Settings:
        # A fresh clone must boot. In development and testing we mint an
        # ephemeral key rather than refusing to start; production insists on a
        # real one, because a rotating key silently invalidates every session.
        if not self.secret_key:
            if self.env == "production":
                raise ValueError(
                    "MASSINGBILL_SECRET_KEY is required in production. "
                    "Generate one with: python -m massingbill.cli gen-secret"
                )
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))

        if not self.database_url:
            path = self.instance_path / "massingbill.sqlite3"
            object.__setattr__(self, "database_url", f"sqlite:///{path.as_posix()}")

        # Plain HTTP in development would otherwise drop the session cookie.
        if self.env == "development":
            object.__setattr__(self, "session_cookie_secure", False)
            object.__setattr__(self, "preferred_url_scheme", "http")

        return self

    @property
    def uses_massing_cloud(self) -> bool:
        """True only when an operator has explicitly opted in."""
        return (
            self.entitlement_provider == "massing_cloud" or self.storage_backend == "massing_vault"
        )

    def flask_config(self) -> dict[str, Any]:
        """Translate settings into the Flask config keys the extensions read."""
        return {
            "ENV": self.env,
            "TESTING": self.env == "testing",
            "DEBUG": self.env == "development",
            "SECRET_KEY": self.secret_key,
            "SQLALCHEMY_DATABASE_URI": self.database_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
            "SESSION_COOKIE_SECURE": self.session_cookie_secure,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "REMEMBER_COOKIE_SECURE": self.session_cookie_secure,
            "REMEMBER_COOKIE_HTTPONLY": True,
            "WTF_CSRF_TIME_LIMIT": None,
            "MAX_CONTENT_LENGTH": self.max_content_length,
            "PREFERRED_URL_SCHEME": self.preferred_url_scheme,
            "RATELIMIT_DEFAULT": self.rate_limit_default,
            "RATELIMIT_STORAGE_URI": self.rate_limit_storage_uri,
            "RATELIMIT_HEADERS_ENABLED": True,
        }


def load_settings(**overrides: Any) -> Settings:
    """Build settings, letting tests inject overrides without touching the env."""
    return Settings(**overrides)
