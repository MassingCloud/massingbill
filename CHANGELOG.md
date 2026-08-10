# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-08-09

The first release intended for real projects. Everything below was built and
tested across P0–P8; this entry records what the release *is*, and the
per-phase notes beneath it record how it got there.

### The product

- **A money engine, not a form filler.** Integer cents in `BIGINT`, basis-point
  percentages, one rounding site per computation, and a penny-reconciliation
  pass so `Σ lines == header` always. The G702/G703 layout is one renderer over
  it, not the thing itself.
- **35 tie-out rules** checked before an application can be submitted, rendered
  as a Reconciliation page no competitor ships. There is no override, on
  purpose.
- **The monthly requisition end to end**: schedule of values with revisions,
  applications for payment, change orders, retainage in four modes, stored
  materials that cannot be billed twice, certification with variance, and
  recorded payments.
- **Around it**: lien waivers with ESIGN/UETA evidence capture, compliance
  documents, subcontracts and sub-billings, and a statutory deadline engine.
- **Documents** in PDF, XLSX with live formulas, CSV and JSON.
- **A REST API** at `/api/massingbill/v1` with OpenAPI 3.1, organization-scoped
  API keys, and outbound webhooks signed byte-compatibly with massing.cloud.
- **A hash-chained audit log** that `massingbill audit verify` walks from cron.

### Standalone, enforced rather than claimed

No account, no licence key, no phone-home, no network egress. Three CI jobs
make it a build failure rather than an opinion: the suite runs with outbound
sockets refused, a job deletes every optional adapter and re-runs the whole
suite, and an import contract forbids the core from importing them.

### What it deliberately will not do

- **Move money.** Recording what arrived is a different business from
  transmitting it, and money transmission means licensing in fifty
  jurisdictions.
- **Charge subcontractors.** The general contractor is the customer. Textura
  charges subs ~0.22% of contract value; we charge them nothing, ever.
- **File, serve or record liens.** It computes deadlines and warns.
- **Invent statutory text.** Prescribed waiver forms ship with empty bodies and
  every deadline rule ships with no day count. Both refuse until somebody reads
  the statute and enters them. A waiver that does not conform can be
  unenforceable and a lien filed one day late is simply gone, so a plausible
  number nobody checked is worse than no number.

### Known limits at 1.0

Stated because a release note that only lists strengths is marketing.

- **48 statutory waiver forms and every deadline rule are unverified.** They
  refuse rather than guess. Filling them in is the operator's job, and the
  screens exist to make it straightforward.
- **ERP integration is file-based only** (Sage 300 CRE, Viewpoint Vista,
  QuickBooks, Textura, GCPay). API adapters are not built. This is the order the
  competitive review recommended — it is how these shops actually integrate —
  but it is a limit, not a complete story.
- **No remote online notarisation.** Several states require notarised waivers;
  those projects cannot close a period entirely in-app.
- **No load test or external penetration test has been run.** The security
  posture in `docs/runbook.md` §7 describes what was built, not what an
  independent party has confirmed.
- **The massing.cloud adapter is not built** (P9, post-1.0 by design). The seams
  and wire conventions it needs are in place; see `docs/massing-integration.md`.

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
