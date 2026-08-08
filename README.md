# Massing Bill

**The monthly requisition, closed out in an afternoon and provably correct.**

Schedule of values, G702/G703-format applications for payment, change orders,
retainage, stored materials and lien waivers — in one auditable engine that a
general contractor can run themselves.

[![CI](https://github.com/MassingCloud/massingbill/actions/workflows/ci.yml/badge.svg)](https://github.com/MassingCloud/massingbill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

---

## Standalone. Actually standalone.

No account. No licence key. No shared secret. No phone-home. No network egress.

```bash
git clone https://github.com/MassingCloud/massingbill.git
cd massingbill
docker compose up
```

That is the whole install — <http://localhost:8000>.

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

**Phase P0 (foundation) is complete.** The application factory, configuration,
adapter seams, security posture, container and CI are in place and green. The
billing engine itself lands in P1–P4.

See [`SPEC.md`](SPEC.md) for the full plan: research, data model, the money
kernel, the tie-out rule reference, the testing strategy and the phase-by-phase
acceptance criteria.

| Phase | Scope | State |
|---|---|---|
| P0 | Foundation, adapters, CI, container | **done** |
| P1 | Money kernel | next |
| P2 | Orgs, RBAC, projects, schedule of values | |
| P3 | The requisition engine (G702/G703, retainage, change orders) | |
| P4 | Tie-out rule engine | |
| P5 | PDF / XLSX / CSV / JSON documents | |
| P6 | Workflow, lien waivers, compliance, subcontractors, e-signature | |
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

`MASSINGBILL_SECRET_KEY` is the one value required in production; the container
refuses to start without it. Generate one with `massingbill gen-secret`.

## A note on the AIA forms

AIA G702® and G703® are copyrighted documents of The American Institute of
Architects, and reproducing them requires a licence from the AIA.

Massing Bill models the **line structure and the arithmetic**, which is not what
copyright protects. It does not reproduce AIA form artwork or certification
wording, and every rendered document carries a disclaimer that cannot be removed
from the AIA-style renderer. A house-style renderer, a custom renderer that maps
onto whatever form your GC requires, and a G703-column-ordered XLSX export (so a
licence holder can populate their own official document) all ship alongside it.

**Massing Bill is not affiliated with, endorsed by, or sponsored by The American
Institute of Architects.** See [`docs/legal-forms-policy.md`](docs/legal-forms-policy.md).

## Licence

MIT — see [`LICENSE`](LICENSE). The licence covers the software; it grants no
rights in any third-party trademark.
