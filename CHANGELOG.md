# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — P0, foundation

- Flask application factory that boots with **no environment file and no
  network**, selecting the standalone entitlement provider and local storage.
- `Settings` (pydantic-settings, `MASSINGBILL_` prefix) where every default works
  offline; production refuses to start without `MASSINGBILL_SECRET_KEY`.
- The three adapter seams — `EntitlementProvider`, `IdentityProvider`,
  `StorageBackend` — with only their standalone implementations. Optional
  adapters are lazily imported and report a useful error when absent.
- `StandaloneProvider`: no licence enforcement, no phone-home, no telemetry.
- `LocalStorage` with path-traversal rejection and atomic writes.
- `LocalPasswordProvider` (argon2id) with timing-equalised unknown-account handling.
- Money and basis-point column helpers, so the integer-cents decision is settled
  before the first table.
- Security posture: strict `default-src 'self'` CSP, CSRF, rate limiting,
  security headers, HMAC-SHA256 webhook signing, digest-only token storage.
- `/healthz` (no database) and `/readyz` (database-checked) probes.
- Structured JSON logging with a request id echoed on every response.
- Alembic migrations, Docker image, docker-compose stack, gunicorn config.
- CI: ruff, ruff format, mypy, pytest on 3.11/3.12/3.13, pip-audit, SBOM, CodeQL,
  Semgrep, migration round-trip, container boot — plus the three
  standalone-integrity gates (`offline`, `no-adapters`, `imports`).

[Unreleased]: https://github.com/MassingCloud/massingbill/compare/main...HEAD
