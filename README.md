# Massing Bill

**The monthly requisition, closed out in an afternoon and provably correct.**

Schedule of values, G702/G703-format applications for payment, change orders,
retainage, stored materials and lien waivers — in one auditable engine that a
general contractor can run themselves.

[![CI](https://github.com/MassingCloud/massingbill/actions/workflows/ci.yml/badge.svg)](https://github.com/MassingCloud/massingbill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**[See a worked six-month project →](https://massingcloud.github.io/massingbill/)**
Real output from the real engine: G702/G703 applications, live-formula
spreadsheets, and the reconciliation page no competitor ships.

---

## Standalone. Actually standalone.

No account. No licence key. No shared secret. No phone-home. No network egress.

```bash
git clone https://github.com/MassingCloud/massingbill.git
cd massingbill
docker compose up
```

That is the whole install — <http://localhost:8000>.

Then seed a worked project to look at:

```bash
docker compose exec app massingbill demo
```

Six periods on a $4,850,000 job, exercising a change order that adds a line,
stored material that later installs, a deductive change order, and an architect
certifying less than was applied for. The same project is published at
[massingcloud.github.io/massingbill](https://massingcloud.github.io/massingbill/)
if you would rather just read it.

Massing Bill *can* connect to [massing.cloud](https://massing.cloud) for
single sign-on, entitlements and vault storage, but every one of those is an
**optional adapter that is off by default**. Three CI jobs enforce it: the test
suite runs with outbound sockets blocked, a job **deletes the adapter modules and
re-runs the suite**, and an import contract forbids the core from importing them
at all. Coupling is a build failure here, not a code-review opinion.

## Why it exists

Pay-app tools produce forms. None of them hands you a **proof that the numbers
tie** — which is what an owner's auditor, a lender's inspector and a suspicious
project accountant actually need.

So the architecture is inverted. The durable asset is a money engine plus a
tie-out rule set; the G702/G703 layout is one of several renderers over it.

- **Money is integer cents in a `BIGINT`.** Never a float. Percentages are basis
  points. Rounding happens at exactly one site per computation, followed by a
  penny-reconciliation pass so `Σ lines == header`, always.
- **Retainage is computed per line and summed** — never computed on the header
  and pushed down, which is the origin of the one-cent disagreements that get pay
  apps rejected.
- **Submitted applications are frozen.** Each carries a hashed JSON snapshot of
  the SOV, retainage rule, change-order log and entitlement, so it re-renders
  byte-identically in five years even after the SOV has moved on.
- **Every number is checked before submit.** `line3 == line1 + line2`,
  `line7 == prior.line6`, `G == D + E + F`, `Σ line retainage == line5`, plus
  policy rules for overbilling, statutory retainage caps, double-billed stored
  materials and missing waivers. The result is a Reconciliation page in the PDF.

## Status

**Phases P0–P5 are complete, and P6 is most of the way there.** The money
kernel, the tenant and authorization model, the period engine, 35 tie-out rules
and the whole document set are built and tested. A ten-period golden project
with hand-computed G702 headers reproduces every figure exactly, every period
passes tie-out, and the [published demo](https://massingcloud.github.io/massingbill/)
is that same engine's real output.

What remains in P6 is the web UI over the workflow services, which exist and are
tested but are reachable today only through the API and the CLI.

See [`SPEC.md`](SPEC.md) for the full plan and the phase-by-phase acceptance
criteria, and [`docs/competitive-upgrades.md`](docs/competitive-upgrades.md) for
the Textura / GCPay / Handle review that shaped the later phases.

| Phase | Scope | State |
|---|---|---|
| P0 | Foundation, adapters, CI, container | **done** |
| P1 | Money kernel | **done** |
| P2 | Orgs, RBAC, projects, schedule of values, audit chain | **done** |
| P3 | The requisition engine (G702/G703, retainage, change orders, stored materials) | **done** |
| P4 | Tie-out rule engine — 35 rules | **done** |
| P5 | PDF / XLSX / CSV / JSON documents | **done** |
| P6 | Lien waivers, e-signature, compliance, subcontracts, payments | **done** |
| P6 | The web UI over them | next |
| P6.5 | Statutory deadline engine | |
| P7 | REST API, webhooks, OIDC, S3, Procore / QuickBooks / Sage | |
| P8 | Hardening, ops runbook, **v1.0.0** | |
| P9–P10 | *Optional:* massing.cloud adapter, WordPress bridge | |

## Development

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest -q
```

The four checks CI runs, which should pass before every commit:

```bash
ruff check . && ruff format --check . && mypy massingbill && pytest -q
```

Plus the import contracts that keep the core decoupled:

```bash
lint-imports
```

PDF rendering needs the WeasyPrint native stack (pango, cairo). Install
`.[render]` and the system libraries, or just use the container — the image
carries them.

## Configuration

Everything has a working default; see [`.env.example`](.env.example) for the
full surface. The three that matter:

| Variable | Default | Meaning |
|---|---|---|
| `MASSINGBILL_ENTITLEMENT_PROVIDER` | `standalone` | `standalone` enforces nothing at all |
| `MASSINGBILL_STORAGE_BACKEND` | `local` | Protected local filesystem |
| `MASSINGBILL_OIDC_PROVIDERS` | *(empty)* | Empty means local password accounts only |

Two values are required in production, and the container refuses to start
without either: `MASSINGBILL_SECRET_KEY` (signs sessions) and
`MASSINGBILL_ENCRYPTION_KEY` (encrypts TOTP seeds and integration tokens at
rest). They are separate on purpose — rotating a session key must not lock every
user out of two-factor. Generate a value for each with `massingbill gen-secret`.

## A note on the AIA forms

AIA G702® and G703® are published by The American Institute of Architects, which
asserts copyright in them and licenses their use.

Massing Bill implements the **line structure and the arithmetic**, and writes
every word itself. That distinction is not wishful: under *Baker v. Selden* and
[37 C.F.R. § 202.1(c)](https://www.ecfr.gov/current/title-37/chapter-II/subchapter-A/part-202/section-202.1),
"blank forms… designed for recording information" are not copyrightable — a
ruled grid follows from the arithmetic it records. What *is* protected is the
expressive prose: certification wording, instructions, artwork. **Every
certification paragraph in this product was written from scratch**, and no AIA
artwork, wording or mark appears in it.

Every rendered document carries a disclaimer that cannot be removed from any
renderer. A house-style renderer, a custom renderer that maps onto whatever form
your GC requires, and a G703-column-ordered XLSX export (so a licence holder can
populate their own official document) all ship alongside it.

**Massing Bill is not affiliated with, endorsed by, or sponsored by The American
Institute of Architects.** See [`docs/legal-forms-policy.md`](docs/legal-forms-policy.md).

## Licence

MIT — see [`LICENSE`](LICENSE). The licence covers the software; it grants no
rights in any third-party trademark.
