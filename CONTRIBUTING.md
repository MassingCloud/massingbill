# Contributing

Thanks for helping. A few things about this codebase will save you time.

## Before you start

Read [`AGENTS.md`](AGENTS.md) — it is short, and it explains the constraints that
are not obvious from the code. [`SPEC.md`](SPEC.md) has the full design and the
phase plan; work generally follows it in order.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Before every commit

```bash
ruff check . && ruff format --check . && mypy massingbill && pytest -q && lint-imports
```

CI runs all of these as blocking jobs, plus the container build and the three
standalone-integrity gates.

## The rules that will get a PR sent back

1. **Floating-point money.** Monetary values are integer cents in a `BIGINT`,
   declared through `models.base.money_column`. Percentages are basis points.
2. **Arithmetic on money outside `services/money.py`.** There is one rounding
   site per computation and one penny-reconciliation pass; scattered arithmetic
   defeats both.
3. **Importing an optional adapter from the core.** Blueprints and models may not
   import `massing_cloud`, `oidc`, `s3` or `integrations`. Add to the ABC instead.
4. **Mutating a submitted application.** They are frozen by design.
5. **Reproducing AIA form artwork or certification wording.** We model the line
   structure and the arithmetic only — see `docs/legal-forms-policy.md`.
6. **A test that needs the network.** The suite blocks outbound sockets. Use
   recorded fixtures.

## Tests

New behaviour needs a test. Changes to the money engine, retainage, the period
engine or the tie-out rules need a test that would have failed before the change
— those four modules carry a 100% coverage requirement, and a pay app that is
wrong by a penny is a pay app that gets rejected.

## Commits and PRs

Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`) are preferred but not enforced. Explain *why* in the body; the diff
already says what.
