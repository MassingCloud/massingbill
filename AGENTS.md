# Massing Bill — Agent Operating Guide

## What this is
A standalone, self-hostable AIA-style GC billing engine: schedule of values,
G702/G703-format applications for payment, change orders, retainage, stored
materials, lien waivers. **Read `SPEC.md` first** — it carries the research, the
form definitions, the data model and the phase plan with acceptance criteria.

## Architecture (do not drift)
- Python 3.11+ · Flask 3 · SQLAlchemy 2.0 · Alembic · Jinja server-rendered UI.
  No SPA. Postgres in production, SQLite in dev and tests.
- **Standalone is the product, not a mode.** Zero runtime dependency on
  massing.cloud. Every external touchpoint is one of three adapter ABCs
  (`services/entitlement`, `services/identity`, `services/storage`), default
  implementation local, optional implementation lazily imported.
- The engine is layered: `models/` → `services/` → `blueprints/`. Blueprints
  never import an adapter implementation or `services/integrations/`.

## Golden rules
1. **Money is integer cents in `BIGINT`.** No float, no `Numeric` on the money
   path. Declare monetary columns only via `models.base.money_column`.
2. **Percentages are basis points** (`int`, 1 bp = 0.01%). `10%` is `1000`.
3. **All money arithmetic goes through `core/money.py`.**
   Round `ROUND_HALF_UP` at exactly one site per computation, then run the
   penny-reconciliation pass so `Σ lines == header`. (`services/money.py` is a
   re-export kept for the sixty-odd modules that import it.)
4. **Retainage is computed per line and summed.** Never header-down.
5. **Submitted applications are immutable.** Freeze a hashed snapshot; column D
   and G702 line 7 are derived from the prior application and written once at
   period open — never editable, never recomputed.
6. **Financial records are never hard-deleted.** `void` is a state.
7. **No adapter may leak into the core.** If you find yourself importing
   `massing_cloud`, `oidc`, `s3` or `integrations` from a blueprint or a model,
   the design is wrong — add a method to the ABC instead.
8. **Never reproduce AIA form artwork or certification wording** (`SPEC.md` 0.2).
   The disclaimer footer is not removable from the `aia_style` renderer.
9. **Statutory content is effective-dated data, not code.** Retainage caps and
   lien-waiver text live in YAML with a citation and an effective date.
10. **`massingbill/core/` has zero runtime dependencies.** Standard library
    only — no Flask, no SQLAlchemy, no third-party package, and no import from
    the rest of `massingbill` except the core itself. If the core needs an enum,
    move the enum in and re-export it from its old home.
    `tests/test_architecture.py` fails the build on a violation; see
    `docs/vendorable-core.md`.

## Where things live (`massingbill/`)
| Path | Contents |
|---|---|
| `core/` | **Zero dependencies.** `money` · `retainage` · `requisition` (G702/G703) · `enums`. Vendorable. |
| `app.py` | The Flask factory. Split out of `__init__` so importing the core does not load Flask. |
| `config.py` | `Settings` (pydantic-settings, `MASSINGBILL_` prefix). Every default works offline. |
| `extensions.py` | `db`, `migrate`, `csrf`, `login_manager`, `limiter` |
| `security.py` | argon2id, signed tokens, HMAC webhook signing, CSP + headers |
| `errors.py` | Typed errors → HTTP/JSON. Status codes mirror the massing convention. |
| `logging_config.py` | JSON logs, request id |
| `models/base.py` | `money_column`, `bp_column`, UUID pk, timestamps |
| `services/entitlement/` | `base` · `standalone` (default) · `massing_cloud` (P9) |
| `services/identity/` | `base` · `local` (default) · `oidc` (P7) |
| `services/storage/` | `base` · `local` (default) · `s3` (P7) · `massing_vault` (P9) |
| `services/integrations/` | Procore, QBO, Sage (P7b). Never imported by the core. |
| `services/apikeys.py` | Minting and verifying `mbil_` keys. Only the digest is stored. |
| `services/webhooks.py` | Signing, queueing, delivery, retry. **Never sends in a request.** |
| `services/events.py` | Every webhook payload shape, in one place. |
| `blueprints/api.py` | `/api/massingbill/v1`. Authenticates to an `ApiPrincipal`, then reuses `rbac.scoped`. |
| `docs/openapi/` | `massingbill-v1.yaml`, CI-checked against the URL map both ways. |
| `sdk/python/` | Dependency-light client. Not packaged with the app. |

## Testability contract
The suite runs **offline and deterministically** on SQLite with zero
infrastructure:
- `tests/conftest.py` blocks outbound sockets. An accidental network call is a
  loud failure, not a slow test.
- Settings are constructed explicitly; a developer's `.env` cannot change a
  result.
- Integration adapters are tested against recorded fixtures. **Never live data.**

## Workflow
Work phase by phase per `SPEC.md` §11. Meet a phase's acceptance criteria, with
tests, before starting the next. Run the four commands below before every commit.

## Commands
```bash
pip install -e ".[dev]"          # install
pytest -q                        # test
ruff check . && ruff format .    # lint + format
mypy massingbill                 # type check
lint-imports                     # decoupling contracts
flask --app wsgi:app db upgrade  # migrate
massingbill check                # boot and report resolved config
docker compose up                # full stack
```

## CI expectations (all blocking)
`lint` (ruff check + ruff format --check + mypy) · `test` on 3.11/3.12/3.13 ·
`offline` (imports and serves with the network dropped) · `no-adapters` (deletes
every adapter, re-runs the suite) · `imports` (import-linter) · `migrations`
(up/down/up plus `flask db check`) · `security` (pip-audit + SBOM) · `docker`
(image builds, boots, answers `/healthz` and `/readyz`) · CodeQL · Semgrep.
