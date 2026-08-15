# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Entitlements are now enforced.** The provider has been constructed at
  startup since 1.2.0 and nothing ever asked it, so every capability flag
  decided nothing. `massingbill/services/limits.py` is now the one module that
  asks, and five flags have call sites: `gc_billing` and `billing_projects` at
  project creation, `gc_billing` and `billing_apps_per_month` at
  `open_period`, `sub_tier_billing` at `subcontracts.create`, and `esign` at
  `waivers.sign`.

  `custom_forms` deliberately has none: nothing can set
  `PrimeContract.default_form_style`, so a gate there would guard an operation
  that does not exist — which reads as covered without being so.

  **A standalone install is unaffected**, and that is the first thing
  `tests/test_limits.py` asserts. An absent limit means *unlimited*, not zero,
  so the permissive default holds by construction rather than by special case.
  No licence, no phone-home, no kill switch (SPEC.md §3.3) is unchanged.

  Refusals are `409` with the limit, the cap and the current count in
  `details`, so an API client can say something useful. A lapsed subscription
  degrades to read-only; exports and documents keep working and nothing is
  deleted. See `docs/runbook.md` §5a.

### Fixed — authentication

- **The session is now renewed on privilege change.** `_finish_login` clears the
  session before `login_user()`, on all three paths — password, MFA challenge
  and the massing.cloud handoff, which share it.

  Narrower than "session fixation" suggests, and worth stating precisely.
  Flask's session is a signed cookie, so an attacker who plants a cookie value
  cannot have it become authenticated: the server issues a *new* signed cookie
  carrying the user id, and the attacker's copy does not have it. The actual
  gap was that `login_user` adds to the existing dict rather than replacing it,
  so any key an attacker got into the pre-authentication session was carried
  into the authenticated one. Clearing first is the standard control and costs
  a CSRF token that Flask-WTF reissues on the next form.

### Changed

- The entitlement provider is consulted **once per organization per request**,
  cached on `g` above the adapter's own TTL cache. Pinned by a test that counts
  calls, because a page with two gates was otherwise two round trips for an
  answer that cannot change in between.

## [1.3.1] — 2026-08-13

Security fixes to the massing.cloud handoff shipped in 1.3.0, found by
reviewing that release rather than by a report. **Anyone running 1.3.0 with the
bridge configured should upgrade.**

### Fixed — authentication

- **A handoff no longer bypasses two-factor.** The callback signed users in
  directly, so a user with TOTP enrolled was authenticated without it. A
  handoff now authenticates one factor and the user is sent to the same
  `/auth/mfa` challenge the password route uses.

  The assertion carries no `acr`, `amr` or AAL claim, so there is nothing in it
  on which to conclude a second factor was used, and
  [NIST SP 800-63C](https://pages.nist.gov/800-63-4/sp800-63c.html) is explicit
  that a relying party does not assume an assurance level that was never
  asserted. If massing.cloud later asserts `amr`
  ([RFC 8176](https://www.rfc-editor.org/rfc/rfc8176.html)), that claim becomes
  the basis for skipping the local challenge.

- **Locked and deactivated accounts can no longer sign in through the bridge.**
  That policy lived inside `attempt_sign_in`, so the handoff re-implemented user
  resolution without it. It is now `accounts.sign_in_blocker()` and every entry
  point consults it.

- The organization named in an assertion is carried across the MFA challenge.
  `_finish_login` otherwise selects the *first* membership, so a user in two
  organizations would verify their code and land in the wrong tenant.

- `next` is validated before redirect. An unvalidated `next` on a successful
  sign-in is a phishing primitive: the link genuinely is this application.
  Pre-existing rather than introduced by the bridge.

- The handoff callback now carries the same rate limit as every other
  authentication route.

### Fixed — adapters

- `MassingVaultStorage.put` treated a `204 No Content` response as a failed
  write, so callers would retry a write that had already succeeded.
- `MassingCloudProvider` re-attempted the network on every call during an
  outage. A 30-second failure cooldown means an outage costs one timeout rather
  than one per call; a test asserts the cooldown lifts, so a brief outage does
  not become a permanent one.
- `massing_handoff.verify` raised `TypeError` rather than `HandoffError` on a
  non-ASCII signature, because `hmac.compare_digest` refuses non-ASCII `str`.
- `AdapterUnavailableError` was unreachable: `accept()` raised a bare
  `ImportError`, so an operator who configured the bridge without installing
  `massingbill[massing]` saw "your link is not valid" rather than the reason.

Every security fix has a test that fails without it, verified by reverting each
fix in turn.

## [1.3.0] — 2026-08-13

### Added — P10, the WordPress bridge

- `services/identity/massing_handoff.py`: verifies the short-lived HMAC-signed
  assertion the massing.cloud bridge mints. Standard library only — it is the
  authentication boundary, and a dependency there is one more thing between a
  contractor and their billing. An optional adapter like the rest.
- The bridge plugin itself lives in the massingcloud repo as
  `plugin/massing-billing`: it serves the three endpoints
  `MassingCloudProvider` calls, and hands a signed-in WordPress user across.

Eighteen tests, one per way somebody could arrive as a user they are not, so a
regression names which door opened. One of them makes **PHP** mint the assertion
and verifies it here, because base64 padding and JSON escaping are exactly where
two languages disagree quietly and the symptom would be an unexplainable
sign-in loop.

- `/auth/massing/callback`, `services/handoff.py` and the `spent_handoffs`
  table complete the round trip: a WordPress user reaches a billing project.

The route **404s unless a shared secret is configured**, so a standalone install
does not advertise an endpoint it cannot honour and a scanner learns nothing
from asking.

Two decisions, both the conservative end of a real choice. **No account is ever
created** — a valid assertion says massing.cloud believes this person is
entitled, not "give this person a login", and auto-provisioning would turn one
leaked secret into an account in every tenant rather than the ability to
impersonate people who already exist. And **every refusal reads identically**:
whether the signature failed, the link expired, or the account does not exist is
indistinguishable from outside, with the reasons going to the log.

Single use is enforced by the primary key, not by a check. Two simultaneous
presentations of the same assertion both try to insert the same `jti` and the
database decides; read-then-insert would let both through, which is exactly the
race a replayed URL creates.

## [1.2.0] — 2026-08-11

### Added — P9, the massing.cloud adapters

- `services/entitlement/massing_cloud.py`: entitlements, plan limits and seat
  claims from massing.cloud. The field names already matched (SPEC.md 3.1), so
  the mapping is a read rather than a translation.
- `services/storage/massing_vault.py`: documents in the massing vault. Writes
  are digest-verified on the way out *and* the way back, because a document
  store is durable but not immutable and a signed lien waiver whose bytes
  changed is the case worth catching.
- Both are optional adapters. The `no-adapters` CI job deletes them along with
  S3, OIDC and the integrations, and the whole suite still passes.

**A network failure must not stop a contractor billing**, and most of the tests
exist to hold that line. An outage serves the last good entitlement for a week;
past that it degrades to **read-only rather than denied**, because refusing
outright would make our outage look like their lapsed subscription. An
unreachable seat service **grants the seat** — locking a project manager out of
a pay application on the 25th because we could not reach a counter makes our
problem theirs.

Not blocked on the tier decision after all: the capability flag *names* are
settled (`gc_billing`, `billing_projects`, `billing_apps_per_month`,
`sub_tier_billing`, `esign`, `custom_forms`), and which tier grants them lives
in massing's `class-tiers.php`, not in this adapter.

### Added — getting statutory content in

- `services/statutory.py` and `massingbill statutory status|export|import`.
  Exports a worksheet of every unverified waiver form and deadline rule, with
  citations, and reads the filled sheet back.

The refusal to invent statutory text stands and is not negotiable. But a
refusal that leaves somebody clicking through six hundred screens is one they
will route around, so this makes entering it a spreadsheet and an import. The
asymmetry is enforced: **nothing here can produce statutory content**, only move
what a person supplied. Every exported cell is empty with no placeholder, a
blank row is skipped rather than read as an answer, a day count without a
citation is refused, and rows belonging to another tenant are ignored.

### Fixed

- The import contract's `ignore_imports` gained the two massing adapters. It
  broke the moment their modules existed, which is exactly what a contract that
  had been passing vacuously should do.

## [1.1.0] — 2026-08-10

### Added — a dependency-free core

- **`massingbill/core/`**: `money`, `retainage`, `requisition` (the G702 header
  and G703 continuation sheet) and `enums`. **Zero runtime dependencies,
  standard library only.** A pay application can now be computed from plain
  dataclasses with no database session in scope.
- `tests/test_architecture.py` fails the build if the core gains a runtime
  dependency, imports the rest of the application, or stops loading with only
  the standard library reachable. A CI job installs nothing at all — not even
  the package — and imports it.
- `scripts/make_vendor_kit.py` and `check_vendor_drift.py`: a vendorable copy
  with a SHA-256 per file and the commit it came from. The generator **refuses
  a dirty tree**; the checker detects drift in both directions, because a file
  *added* to a vendored copy is the local fix upstream never hears about.
- `massingbill/core/tests/test_mb_*.py`: the suite that travels with the core.
  Deliberately not pytest — plain `assert` and a `__main__` runner — so it runs
  in a consuming repo's harness whatever that harness is.

### Changed

- The Flask factory moved to `massingbill/app.py`. `massingbill/__init__.py`
  resolves `create_app` through PEP 562, so every caller and the console entry
  point are unaffected. Without this, importing `massingbill.core` ran
  `__init__` and loaded Flask — the property was false before anyone tried to
  vendor it.
- `RetainageMode` is defined in `core/enums.py` and re-exported by
  `models/project.py`, so there is exactly one definition and a persisted value
  cannot drift from the value the engine branches on.
- `services/retainage.py` is now an adapter: it turns the stored
  `RetainageRule` row into a plain `RetainageSpec` and calls the core.
- `services/money.py` re-exports the core's `__all__` rather than keeping a
  second list of names.
- The money-discipline gate follows the kernel to `core/money.py`. It had
  exempted `services/money.py` by path, so it flagged the kernel the moment the
  kernel moved.

### Fixed

- `massingbill.core` raises a plain "needs Python 3.11 or newer" instead of
  surfacing `cannot import name 'StrEnum' from 'enum'`, which pointed at the
  standard library rather than at the version. Found by using the vendor kit
  the way a consumer would rather than by reading it.

Nothing in the requisition arithmetic changed. The full suite passes unchanged,
which is the point: this is a move, not a rewrite.

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
