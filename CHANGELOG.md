# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — P1, the money kernel

- `massingbill/services/money.py`: `Cents`/`Bp` newtypes, `apply_bp`,
  `percent_of`, `allocate`, `split_evenly`, `parse_money`, `to_display`,
  `to_decimal`, and the basis-point conversions. Pure integer arithmetic with no
  framework imports, so it is exhaustively testable and could be lifted out
  wholesale.
- `allocate` uses largest-remainder apportionment: a total split across lines
  sums back to the total exactly, with the residual cents landing
  deterministically rather than vanishing.
- Rounding is half-away-from-zero at a single site per computation, so a credit
  rounds by the same magnitude as the charge it reverses.
- 200-case (local) / 1,000-case (CI) / 10,000-case (thorough) hypothesis
  profiles, plus a seeded 10,000-case allocation sweep that is identical on
  every machine.
- CI job `money-kernel`: 100% coverage requirement on the kernel, the thorough
  property profile, and the money-discipline AST gate.

### Fixed — P1

- `allocate` was not sign-symmetric. Floor division sent the residual cent the
  other way for a negative total, so `allocate(1, [1, 1])` put the odd cent on
  line 1 while `allocate(-1, [1, 1])` put it on line 2 — a change order and the
  credit reversing it would land on different lines, leaving two SOV lines each
  a cent adrift while the totals still tied. Found by the property suite.
- `parse_money` rejected amounts containing a non-breaking, narrow no-break or
  thin space as the thousands separator — routine in spreadsheet and
  locale-formatted input, and invisible on screen, so the resulting "not a
  monetary amount" was undebuggable.

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
