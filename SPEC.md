# Massing Bill — Build Specification

> **Repo:** [`MassingCloud/massingbill`](https://github.com/MassingCloud/massingbill) → local `C:\Server\massingbill`
> **Product:** *Massing Bill* — the AIA-style GC billing engine (G702/G703, SOV,
> change orders, retainage, lien waivers, the whole monthly requisition).
> **Licence:** MIT · **Status:** specification. Nothing built yet.
>
> **A standalone product.** Zero runtime dependency on massing.cloud — no account,
> no licence key, no shared secret, no network egress. `git clone && docker compose
> up` gives you the whole system. Massing compatibility is achieved through **wire
> conventions** (§3.1) and **three optional adapters** (§3.2), never through
> coupling — and CI enforces that (§13).

This document is the plan. It is agent-ready: every phase in §11 has concrete
deliverables and acceptance criteria that can be self-checked. Read §1–§4 before
writing any code; §5 (money) and §6 (tie-out rules) are load-bearing and must not
be improvised.

---

## 0. Research that shaped this plan

### 0.1 The four reference repositories

| Repo | Stack | What it actually is | What we take | What we reject |
|---|---|---|---|---|
| [`ibuilder/ProcoreAPI-PHPInvoice`](https://github.com/ibuilder/ProcoreAPI-PHPInvoice) | Plain PHP + Composer, session auth, Excel out | Thin Procore→Excel G702/G703 generator. `src/ProcoreApi.php`, `src/AiaGenerator.php`, `templates/*.php`. Credentials in `config/config.php`. | The **Procore-budget-as-SOV-source** idea, and the insight that XLSX (not just PDF) is what accounting actually wants. | Secrets in a config file; no persistence; no period history; no retainage model. |
| [`ibuilder/gcbilling`](https://github.com/ibuilder/gcbilling) (**archived 2025-06-15**) | Laravel + Tailwind + Vite, PHPUnit | The most complete of the four: SOV, applications for payment, change orders, GMP budget, general conditions, org charts, PDF+XLSX export. | The **feature surface** — this is the right scope. GMP/general-conditions tracking is a genuine GC need most tools miss. | Framework (Laravel), and the "AIA G702/G703 Data Generation" logic living in an ad-hoc library instead of a tested money engine. |
| [`ibuilder/gcbill`](https://github.com/ibuilder/gcbill) (**archived 2025-05-11**) | Hand-rolled PHP MVC + MySQL, PhpSpreadsheet, Dompdf | Earlier cut of the same idea. Entities: `Users`, `Projects`, `Schedule_of_Values`, `Applications_for_Payment`, `Application_Payment_Details`, `Change_Orders`, `User_Permissions`. All AIA math in one `AIA.php`. | The **entity decomposition** — an application header row plus a per-line detail row per period is exactly right and we keep that shape. | Its own README concedes "full standards compliance remains a planned enhancement." That gap is the whole product. |
| [`ibuilder/bill-it-construct`](https://github.com/ibuilder/bill-it-construct) (**archived 2025-05-11**) | React + TS + Vite + shadcn-ui, 2 commits, Lovable-generated | A UI shell. No billing logic, no schema, no AIA content. | Nothing structural. | Everything — it is a scaffold, not a system. |

**The conclusion from all four:** every prior attempt modeled the *forms* and then
bolted arithmetic on. All four are archived. The durable asset is not the form —
it is a **provably correct money engine plus a tie-out rule set**, with the forms
as one of several renderers. That inversion is the architecture below.

### 0.2 The legal constraint that governs the output layer

AIA G702®/G703® are **copyrighted works of the American Institute of Architects.**
The purchaser of a document gets a limited license to reproduce roughly ten copies
of a *completed* form for one specific project; reproduction beyond that requires
written AIA permission ([AIA Contract Documents instruction sheets](https://contractdocshelp.aia.org/Get_Document_Answers/Document_Instruction_Sheets/By_Series/G-Series/G702S-2017.htm)).
Every commercial competitor therefore ships **"AIA-style"** output and states
plainly that it is not affiliated with or endorsed by the AIA.

**Our policy — non-negotiable, enforced in code review and documented in
`docs/legal-forms-policy.md`:**

1. We ship an **`aia_style` renderer**: our own typography and layout, carrying the
   same data, the same line numbering, and the same arithmetic. We never copy AIA
   form artwork, boilerplate certification wording, or the AIA logo/registered marks.
2. Every rendered PDF/XLSX carries a footer: *"Prepared with Massing Bill. Format
   follows the AIA G702/G703 line structure. Massing Bill is not affiliated with,
   endorsed by, or sponsored by The American Institute of Architects. AIA®, G702®
   and G703® are registered trademarks of the AIA."* The footer is not removable
   from the `aia_style` renderer.
3. We ship a **`house` renderer** (clean-sheet Massing-branded form) and a
   **`custom` renderer** (map our fields onto a GC- or owner-supplied template —
   the Siteline model), so no customer is ever forced through the AIA-shaped one.
4. We ship an **XLSX/CSV/JSON export whose column order matches the G703 columns**,
   so a customer holding a real AIA licence can populate their own licensed
   document. This is the "safe path" and it is documented as such.

We are modeling a *numbering convention and an arithmetic*, which is not
copyrightable. We are not reproducing a *form*.

### 0.3 The forms, precisely (this is the spec for §5 and §6)

**G703 continuation sheet — columns**
([AIA instructions](https://help.aiacontracts.com/hc/en-us/articles/1500009308302-Instructions-G703-1992-Continuation-Sheet),
[Procore](https://www.procore.com/library/aia-g703-continuation-sheet),
[Knowify](https://knowify.com/resources/aia-g703/)):

| Col | Name | Definition / formula |
|---|---|---|
| A | Item No. | SOV line identifier (we also carry CSI MasterFormat code + cost code) |
| B | Description of Work | SOV line description |
| C | Scheduled Value | This line's share of the Contract Sum **to date** (base + approved COs) |
| D | Work Completed — From Previous Application | Prior application's `D + E` for this line. **Derived, never typed.** |
| E | Work Completed — This Period | Value of work completed this period, incorporated into the work. Excludes stored materials. |
| F | Materials Presently Stored | Value of stored materials **not** already in D or E |
| G | Total Completed and Stored to Date | `D + E + F` |
| % | Percent complete | `G / C` |
| H | Balance to Finish | `C − G` |
| I | Retainage | Only for **variable / line-item** retainage contracts; blank when retainage is a flat contract-level rate |

**G702 application cover — lines**
([AIA](https://help.aiacontracts.com/hc/en-us/articles/1500009308262-Instructions-G703S-2017-Continuation-Sheet-Contractor-Subcontractor-Version),
[Autodesk](https://www.autodesk.com/blogs/construction/g702-g703-forms-aia-billing/),
[Trimble](https://www.trimble.com/blog/construction/en-US/article/a-quick-guide-to-g702-g703-aia-documents)):

| Line | Label | Formula |
|---|---|---|
| 1 | Original Contract Sum | Prime contract award amount |
| 2 | Net change by Change Orders | Σ approved CO additions − Σ approved CO deductions |
| 3 | Contract Sum to Date | `1 + 2` |
| 4 | Total Completed & Stored to Date | Σ G703 Column G |
| 5a | Retainage — % of Completed Work | `rate_work × Σ(D + E)` |
| 5b | Retainage — % of Stored Material | `rate_stored × Σ F` |
| 5 | Total Retainage | `5a + 5b`, **or** Σ Column I when variable retainage is in effect |
| 6 | Total Earned Less Retainage | `4 − 5` |
| 7 | Less Previous Certificates for Payment | Prior application's **Line 6**. Derived, never typed. |
| 8 | Current Payment Due | `6 − 7` |
| 9 | Balance to Finish, Including Retainage | `3 − 6` |

Plus the **Change Order Summary** box (additions/deductions *approved in previous
months* and *approved this month*, netting to Line 2), the contractor's
certification + notary block, and the architect's certificate with **Amount
Certified** (which may differ from Line 8 — we model both and track the delta).

### 0.4 Retainage and payment law (the compliance engine's inputs)

- Retainage caps split roughly between **5%** and **10%** states; **New Mexico**
  bars retainage on most projects
  ([Construction Coverage](https://constructioncoverage.com/glossary/retainage)).
- **California SB 61**, effective **2026-01-01**, caps retention on **private**
  projects at **5%**, with a **2%/month penalty** plus fees for improper
  withholding; residential is excluded unless mixed-use or >4 stories
  ([Buchalter](https://www.buchalter.com/insights/effective-january-1-2026-california-sb-61-caps-retention-at-5-on-private-construction-projects/),
  [Holland & Knight](https://www.hklaw.com/en/insights/publications/2026/04/california-expands-5-percent-retainage-cap-to-private-projects)).
- **New York**: 5% cap for contracts on/after 2023-11-17. California release
  timing: retention to a direct contractor within **45 days** of completion;
  contractor→sub within **10 days** of receipt
  ([Levelset](https://www.levelset.com/retainage/california-retainage-faqs/)).
- **Twelve states mandate statutory lien-waiver forms** — CA, TX, FL, GA, MS, AZ,
  NV, UT, WY, MA, MI, MO. Deviation from the prescribed text can void the waiver;
  FL permits alternates, MO's applies to residential only
  ([Levelset](https://www.levelset.com/blog/lien-waivers-12-states-with-required-forms/),
  [GCPay](https://ww3.gcpay.com/blog/lien-waiver-requirements-by-state/)).
  Four waiver types each: conditional progress, unconditional progress,
  conditional final, unconditional final.

**Design consequence:** retainage rules and waiver texts are **versioned data with
an effective date and a statutory citation**, seeded from YAML, never hardcoded in
Python. A pay app renders the waiver text that was in force on its period end
date. This is how we survive the next SB 61.

### 0.5 Competitive read (what "production ready" has to mean here)

Textura, GCPay and Siteline all converge on the same feature set: pay-app
generation, **lien-waiver collection across sub-tiers**, compliance-document
tracking (COI, certified payroll), notarization, and e-signature, with real-time
waiver/invoice status ([Oracle Textura](https://www.oracle.com/construction-engineering/textura-construction-payment-management/),
[GCPay](https://ww3.gcpay.com/how-gcpay-outperforms-textura-for-payment-solutions/),
[Siteline](https://www.siteline.com/faq)). Siteline's specific wedge is
*"generating the exact forms your GCs require"* — i.e. custom form mapping.

Two structural gaps we exploit:

1. **Nobody sells the tie-out.** These tools produce forms; they do not hand you a
   signed, reproducible proof that every number ties. That is what makes an owner's
   auditor and a lender's inspector sign off, and it is cheap for us because it
   falls out of a well-built money engine (§6).
2. **Nobody is a self-hostable Python engine with a real API.** Consistent with
   [Osprey](../osprey/SPEC.md)'s positioning, that is a durable advantage with GCs
   who have data-residency or NDA constraints.

### 0.6 Procore, as an integration target

- `GET/POST /rest/v1.0/.../payment_applications` — **Payment Applications (Owner
  Invoices)** = our outbound prime pay app
  ([docs](https://developers.procore.com/reference/rest/payment-applications-owner-invoices)).
- `.../requisitions` — **Requisitions (Subcontractor Invoices)** = inbound sub
  billings for the AP side
  ([docs](https://developers.procore.com/reference/rest/requisitions-subcontractor-invoices?version=1.0),
  [tutorial](https://developers.procore.com/documentation/tutorial-requisitions)).
- `requisition_subcontractor_invoice_contract_items` — the line-item grid.
- `contract_payments`, `prime_contracts`, `purchase_order_contracts`, `budget`.
- OAuth 2.0 authorization-code + client-credentials
  ([endpoints](https://developers.procore.com/documentation/oauth-endpoints)).

**We do not write a new Procore client.** `C:\Server\scopemaker` already ships
`scopemaker/services/procore_client.py`, `procore_sync.py`, a `procore` blueprint,
`models/procore.py` and `tests/test_procore.py`. Phase P6 lifts that code, generalizes
it, and — if it proves stable — extracts it to a shared `massing-procore` package
consumed by both repos.

---

## 1. Product definition

**One-line pitch:** *The monthly requisition, closed out in an afternoon and
provably correct — G702/G703, SOV, change orders, retainage, stored materials and
lien waivers in one auditable engine that GCs can self-host.*

### 1.1 Who it serves

| Role | Job to be done |
|---|---|
| **GC project accountant** | Build the SOV, run the month, catch mistakes *before* the owner does, issue the pay app package. |
| **GC project manager** | Enter percent-complete, justify stored materials, keep the CO log tied to the SOV. |
| **GC controller / CFO** | See billed vs earned vs retained across the portfolio; forecast retainage release; export to the ERP. |
| **Subcontractor** | Bill the GC on the GC's own form; return signed lien waivers without a fax machine. |
| **Owner / owner's rep / architect** | Review, certify (an **Amount Certified** that may differ from the request), and see exactly why a number changed. |
| **Lender / inspector / auditor** | Read the tie-out report and the immutable audit trail. |

### 1.2 Scope

**In scope, v1.0:** projects & prime contracts · schedule of values (import,
build, CSI-coded, GMP mode, unit-price mode) · change order log (PCO → CO,
allowances, CCDs) · monthly applications with full period locking · retainage
(flat, split work/stored, variable per line, reduction at milestone, release) ·
stored materials (on-site/off-site/bonded, with backup) · G702-style + G703-style
+ house + custom renderers → PDF/XLSX/CSV/JSON · the tie-out rule engine ·
lien waivers for the 12 statutory-form states + a general form · compliance
document tracking (COI, certified payroll, W-9, bonds) · subcontract commitments
and **sub-tier pay apps** (the AP side) · approval workflow with e-signature ·
audit log · REST API · Procore + QuickBooks Online + Sage export · RBAC ·
multi-tenant orgs · local accounts with argon2id + TOTP.

**Optional, off by default (§3.2):** OIDC/SSO providers (massing.cloud,
Microsoft, Google, Procore, Autodesk, any OIDC IdP) · S3/R2 or Massing Vault
storage · massing.cloud entitlement enforcement. **None of these is required to
run, and the full test suite passes with all of them disabled and the network
blocked.**

**Explicitly out of scope, v1.0:** general ledger / full accounting, payroll
processing, actual money movement (ACH/payment rails), bid management, scheduling,
takeoff/estimating, notarization *as a service* (we produce the notary block and
integrate an e-notary vendor later, we do not become one).

---

## 2. Architecture

**Framework decision: Flask.** Requested by the user, and it matches the closest
sibling in the estate — `C:\Server\scopemaker` is Flask 3 + Flask-SQLAlchemy +
Flask-Migrate + Flask-Login + Flask-WTF + Flask-Limiter + WeasyPrint + Authlib +
argon2 + pyotp/segno, ruff + mypy + pytest, gunicorn + Docker. Massing Bill is a
**deliberate clone of that skeleton**, which buys us a known-good CI shape, a
known-good CSP, an existing Procore client and an existing PDF pipeline.

(For contrast: `osprey/backend` is FastAPI + Postgres/pgvector + ARQ because it is
an always-on async ingest engine. Massing Bill is a request/response document
system with heavy server-rendered forms — Flask is the right half of the estate to
copy.)

Solid lines are the standalone system. Everything below the dashed line is an
**optional adapter** — unplug all of it and the product is unchanged.

```
                                 ┌──────────────────────────────────────────┐
   Browser (server-rendered      │            MASSING BILL (Flask)          │
   Jinja + htmx, no SPA)  ──────▶│                                          │
                                 │  blueprints/   auth · projects · sov ·   │
   Sub portal (magic-link) ─────▶│                applications · changes ·  │
                                 │                waivers · compliance ·    │
   Owner/architect (link) ──────▶│                subs · approvals · api ·  │
                                 │                admin · exports           │
   REST clients (Bearer) ───────▶│        │                                 │
                                 │        ▼                                 │
                                 │  services/                               │
                                 │   money.py      ← Decimal/cents kernel   │
                                 │   sov.py        ← SOV + CO application   │
                                 │   application.py← period engine (G702/03)│
                                 │   retainage.py  ← rule-driven withholds  │
                                 │   tieout.py     ← THE RULE ENGINE (§6)   │
                                 │   waivers.py    ← statutory text, dated  │
                                 │   compliance.py ← COI/CP/W-9/bond expiry │
                                 │   signature.py  ← ESIGN/UETA evidence    │
                                 │   renderers/    ← pdf · xlsx · csv · json│
                                 │        │                                 │
                                 │        ▼                                 │
                                 │  models/ (SQLAlchemy 2.0, Alembic)       │
                                 └────┬────────────────────┬────────────────┘
                                      │                    │
                    ┌─────────────────┘                    │
                    ▼                                      ▼
          Postgres 16 (prod)                    LocalStorage (default,
          SQLite (dev + tests)                  protected filesystem)

    ═════════════ everything below is OPTIONAL, off by default ═════════════

     identity/            entitlement/          storage/         integrations/
     OidcProvider         MassingCloud          S3Storage        procore · qbo
     (massing.cloud,      Provider              MassingVault     sage · textura
      MS, Google,         (validate +           Storage          (all opt-in,
      Procore, Okta…)      addon: merge)                          fixture-tested)
```

### 2.1 Package layout (mirrors scopemaker exactly)

```
massingbill/
├── massingbill/
│   ├── __init__.py            app factory: create_app(config)
│   ├── config.py              pydantic-settings, MASSINGBILL_ env prefix
│   ├── extensions.py          db, migrate, login, csrf, limiter, mail
│   ├── errors.py              typed exceptions → HTTP + JSON problem details
│   ├── security.py            argon2, CSP, headers, signed tokens, RBAC decorators
│   ├── logging_config.py      structured JSON logs, request id, no secrets
│   ├── cli.py                 `massingbill` CLI: seed, demo, tieout, export, migrate
│   ├── models/
│   │   ├── base.py            money columns, TimestampMixin, soft delete, UUID pk
│   │   ├── organization.py    Organization, Membership, Role, Invitation
│   │   ├── user.py            User (argon2 + TOTP), ApiKey, SsoIdentity
│   │   ├── project.py         Project, PrimeContract, ContractParty, RetainageRule
│   │   ├── sov.py             ScheduleOfValues, SovLine, SovRevision, CostCode
│   │   ├── change.py          ChangeOrder, ChangeOrderLine, PotentialChangeOrder
│   │   ├── application.py     Application, ApplicationLine, ApplicationSnapshot,
│   │   │                      Certification, PeriodLock
│   │   ├── stored.py          StoredMaterial, StoredMaterialBacking
│   │   ├── subcontract.py     Subcontract, SubcontractLine, SubApplication
│   │   ├── waiver.py          WaiverTemplate, WaiverInstance, WaiverSignature
│   │   ├── compliance.py      ComplianceDoc, ComplianceRequirement
│   │   ├── document.py        RenderedDocument, StoragePointer, ShareToken
│   │   ├── workflow.py        ApprovalStep, ApprovalAction, Signature
│   │   └── audit.py           AuditEvent (hash-chained), TieoutReport
│   ├── services/              core engine (as in the diagram) plus the three
│   │   ├── entitlement/       base.py · standalone.py (DEFAULT) · massing_cloud.py
│   │   ├── identity/          base.py · local.py (DEFAULT) · oidc.py
│   │   ├── storage/           base.py · local.py (DEFAULT) · s3.py · massing_vault.py
│   │   └── integrations/      procore/ · qbo/ · sage/ · textura/   (all opt-in)
│   ├── blueprints/            (as in the diagram; each with forms.py + views.py)
│   ├── data/seed/
│   │   ├── retainage_rules/*.yaml    per-state, effective-dated, cited
│   │   ├── waivers/*.yaml            12 statutory states + general, versioned
│   │   ├── masterformat.yaml         CSI divisions/sections (shared w/ scopemaker)
│   │   └── demo/*.yaml               the 12-month golden project (§7.2)
│   ├── templates/             Jinja: app UI + document templates
│   └── static/
├── migrations/                Alembic
├── tests/                     see §7
├── docs/                      see §12
├── deploy/                    Dockerfile, docker-compose.yml, gunicorn.conf.py,
│                              docker-entrypoint.sh, k8s/, .env.example
├── .github/workflows/         ci.yml · codeql.yml · semgrep.yml · release.yml · pages.yml
├── pyproject.toml             ruff + mypy + pytest + coverage config
├── SPEC.md  CLAUDE.md  AGENTS.md  README.md  CHANGELOG.md  CONTRIBUTING.md
├── SECURITY.md  LICENSE
```

### 2.2 Dependencies (pinned to the scopemaker green set where they overlap)

```
Flask>=3.0,<4        Flask-SQLAlchemy>=3.1   Flask-Migrate>=4     Flask-Login>=0.6.3
Flask-WTF>=1.2       Flask-Limiter>=3.5      WTForms>=3.1         SQLAlchemy>=2.0,<3
alembic>=1.13        argon2-cffi>=23.1       Authlib>=1.3         requests>=2.31
WeasyPrint>=61       openpyxl>=3.1           python-docx>=1.1     bleach>=6.1
PyYAML>=6            python-dotenv>=1        pydantic>=2.6        pydantic-settings>=2
cryptography>=42     email-validator>=2.1    python-slugify>=8    pyotp>=2.9  segno>=1.6
[postgres] psycopg[binary]>=3.1     [server] gunicorn>=21.2
[dev] pytest pytest-cov ruff mypy hypothesis responses freezegun pypdf
      types-PyYAML types-requests types-bleach pip-audit cyclonedx-bom
```

`openpyxl` is the one addition over scopemaker (XLSX with live formulas — §8.2).
`hypothesis` is the second (property tests on the money engine — §7.3).

---

## 3. Standalone first, massing-compatible by construction

**This is the governing constraint of the whole project, and it is stronger than
"it should also work alone."**

> **The rule:** Massing Bill has **zero runtime dependency on massing.cloud.**
> `git clone && docker compose up` yields a fully functional, production-grade GC
> billing system — accounts, projects, SOVs, applications, tie-outs, PDFs, XLSX,
> waivers, e-signature, API — with no massing account, no licence key, no shared
> secret, no network egress at all. Every massing touchpoint is an **optional
> adapter, disabled by default, behind a feature flag.**

### 3.0 Why "aligned" and "coupled" are different things

The original ask was *"standalone, to be referenced to massing, and eventually
brought back into massing."* Those three are only compatible if alignment is
achieved through **shape**, not through **runtime calls**. So:

| | Standalone core (always) | Optional massing adapter (off by default) |
|---|---|---|
| **Identity** | Local accounts: argon2id + TOTP, invitations, RBAC | `massing_sso` OIDC/PKCE provider added to the provider list |
| **Entitlement** | `StandaloneProvider` — limits from `config`/env, default unlimited | `MassingCloudProvider` — plan + `addon:` merge, seat claim |
| **Storage** | `local` protected filesystem | `s3` · `massing_vault` signed pointer |
| **Documents** | Rendered and stored locally | Additionally pushed to the customer's vault |
| **Events** | Local audit log; webhooks to any URL the customer configures | massing.cloud registered as one more webhook subscriber |
| **Cost data** | Not required | `massing/v1` cost/zoning datasets can enrich SOV cost codes |

Alignment is preserved instead through **five wire-level conventions that cost
nothing to honor and everything to retrofit** (§3.1). That is what makes the
eventual fold-back a configuration change rather than a rewrite.

### 3.1 The five conventions we honor unconditionally

These are followed even when running fully standalone, because they are free now
and expensive later. Sources: `massingcoud/docs/05-rest-api.md`,
`11-cloud-integration-spec.md`, `18-sso-and-desktop-auth.md`,
`12-standards-and-production-plan.md`, `docs/openapi/massing-v1.yaml`, `sdk/python/`.

| Massing convention | How Massing Bill honors it — with no massing dependency |
|---|---|
| **REST namespace + envelope** (`massing/v1`, `massing-vault/v1`) | We expose **`/api/massingbill/v1/...`**: same resource-noun style, same envelope, same error-code table (`401` bad secret, `404` not found, `409` limit, `429` rate limit). Standalone users get a good API; massing gets a familiar one. |
| **Auth header shape** — `Authorization: Bearer <key>` / `X-Api-Key` | Identical. Org-scoped keys, hashed at rest, prefix-visible (`mbil_…`), per-key rate limits + access log, self-service key management. This is just good API design; it happens to match massing-data-core. |
| **Webhook signing** — hex HMAC-SHA256 in `X-Massing-Signature` | Byte-identical scheme for **all** outbound webhooks regardless of subscriber. Events: `application.submitted`, `application.certified`, `application.paid`, `waiver.signed`, `co.approved`, `tieout.failed`. Delivery log + exponential-backoff retry (the `massing_webhook_log` pattern, `docs/12` §P2). A standalone customer points these at their own ERP; massing is later just another subscriber. |
| **Entitlement object shape** (`docs/11` §4) | Our internal capability object uses massing's field names and semantics (`tier`, `entitled`, `limits{}`, `seats{limit,used}`, `expires_at`). `StandaloneProvider` synthesizes one from local config. Nothing in the app knows where it came from. |
| **Secrets from env, never DB** (`docs/12` §4.1) | All secrets via `MASSINGBILL_*` env vars; masked in UI (last 4); never logged; integration tokens AES-GCM encrypted at rest (osprey token-vault pattern). |

Plus two housekeeping conventions: **OpenAPI 3.1** (`docs/openapi/massingbill-v1.yaml`,
same header style as `massing-v1.yaml`, CI-validated, with a dependency-light
`sdk/python/` client mirroring `massing_data/client.py`), and **docs numbering**
(`docs/01-…`, matching the massingcloud `docs/` convention).

### 3.2 The three pluggable seams (this is the entire integration surface)

Everything massing-related is confined to three ABCs. Nothing else in the codebase
imports anything massing-shaped. If the massing adapters were deleted, the app
would still pass its full test suite.

```python
# services/entitlement/base.py
class EntitlementProvider(ABC):
    def effective(self, org: Organization) -> Entitlement: ...
    def claim_seat(self, org, user, instance) -> SeatResult: ...


class StandaloneProvider(EntitlementProvider):  # DEFAULT
    """Limits come from config. Ships unlimited; a self-hoster never sees a gate."""


class MassingCloudProvider(EntitlementProvider):  # opt-in via MASSINGBILL_ENTITLEMENT_PROVIDER
    """POST /massing/v1/validate + GET /entitlement; plan + addon: merge per docs/11 §6."""


# services/identity/base.py  → LocalPasswordProvider (default) | OidcProvider
#   OidcProvider is generic: massing.cloud, Microsoft, Google, Procore, Autodesk,
#   Okta, or any OIDC IdP. massing.cloud is one config block, not a special case.

# services/storage/base.py   → LocalStorage (default) | S3Storage | MassingVaultStorage
```

Config, in full:

```bash
MASSINGBILL_ENTITLEMENT_PROVIDER=standalone   # | massing_cloud
MASSINGBILL_STORAGE_BACKEND=local             # | s3 | massing_vault
MASSINGBILL_OIDC_PROVIDERS=                   # empty = local accounts only
# Only when the massing adapters are switched on:
MASSINGBILL_MASSING_BASE_URL=
MASSINGBILL_MASSING_SHARED_SECRET=
MASSINGBILL_MASSING_LICENSE_KEY=
```

**Test rule (enforced in CI):** the default test suite runs with every adapter set
to standalone and **network access blocked at the socket layer**. Massing adapter
tests live in `tests/integrations/massing/` and run against recorded fixtures. A
test that needs a live massing instance does not exist.

### 3.3 Licensing posture while standalone

Standalone means **no licence enforcement**: `StandaloneProvider` returns
unlimited limits and `entitled=True`. There is no phone-home, no telemetry, no
kill switch, no nag. MIT licence, self-hostable, complete. Gating exists only when
an operator *chooses* `massing_cloud`, and even then it degrades exactly as
`docs/11` §8 specifies — `on-hold` → read-only grace, financial records never
deleted.

This also **removes the last blocker from P0**: the tier/add-on placement question
is now a massing storefront decision that can be made any time before the optional
integration phase, and it blocks nothing.

### 3.4 The path back into massing (deferred, not designed away)

| Option | Verdict |
|---|---|
| Port the engine to PHP inside `plugin/massing-billing` | **No.** Reimplementing a money engine in a second language doubles the surface where a penny can go wrong. |
| Run Flask as a sidecar; a thin WP plugin proxies UI + REST | **Yes, when the time comes.** |
| Keep it separate forever | Also fine — §3.2 means this costs us nothing. |

When it happens, `plugin/massing-billing` is a **thin bridge**: signed-iframe or
server-side-fetched view of the Flask service, WordPress user forwarded via the
massing-sso broker, calls to `/api/massingbill/v1` with the shared secret. It is
the same relationship massing.cloud already has with the app per `docs/11` — the
store owns billing/entitlements, the service owns the domain. **No work in P0–P8
is contingent on this ever happening.**

<details>
<summary>Reference: the full massing convention mapping (for the future bridge)</summary>

| Massing convention | Mapping |
|---|---|
| **REST namespace** `massing/v1`, `massing-vault/v1`, `massing-sso/v1` | We expose **`/api/massingbill/v1/...`** and, when proxied through WordPress, `/wp-json/massingbill/v1/...`. Same resource-noun style, same envelope. |
| **Auth: `Authorization: Bearer <api_key>`** or `X-Api-Key` (data platform) | Identical. Org-scoped keys, hashed at rest, prefix-visible (`mbil_…`), per-key rate limits + access log, self-service key management — a direct port of the massing-data-core pattern. |
| **Signed server-to-server: `X-Massing-Secret`** | Accepted on our signed endpoints for cloud→service calls. |
| **Outbound webhooks: hex HMAC-SHA256 in `X-Massing-Signature`** | Byte-identical scheme. Events: `application.submitted`, `application.certified`, `application.paid`, `waiver.signed`, `co.approved`, `tieout.failed`. Delivery log + exponential-backoff retry (the `massing_webhook_log` pattern from `docs/12` §P2). |
| **Entitlement object + plan/`addon:` merge** (`docs/11` §4, §6) | We implement the exact `effective()` merge, `RANK = {free:0, home:1, commercial:2, enterprise:3}`. New capability flags proposed for `massing-subscriptions/includes/class-tiers.php`: `gc_billing` (bool), `billing_projects` (int, −1=∞), `billing_apps_per_month` (int), `sub_tier_billing` (bool), `esign` (bool), `custom_forms` (bool). New add-on tier string **`addon:gc_billing`**. |
| **Runtime seat check** `POST /wp-json/massing/v1/validate` | Called at session start with `claim_seat: true`; effective entitlement cached locally so feature gates never hit the store per-request. Handle `409 seat_limit` and `429`. |
| **Lifecycle** `active / on-hold / pending-cancel / cancelled` (`docs/11` §8) | `on-hold` → **read-only grace**: existing apps viewable and exportable, no new applications. Never delete billing data — it is a financial record. Retention/archival documented. |
| **SSO** — massing.cloud as OIDC/PKCE broker (`docs/18`) | Second auth path alongside local accounts: `/massing-sso/authorize` → `POST /wp-json/massing-sso/v1/token` → `GET /userinfo`. Loopback restricted to `127.0.0.1`/`localhost`/`[::1]`; state single-use; refresh rotates. `SsoIdentity` links to the local `User`. |
| **Vault** as the document store (`docs/09`) | `StoragePointer` adapter set = `local` (default, protected) · `s3` (SigV4 presigned) · `massing_vault` (signed pointer). Every rendered pay-app package can be pushed into the customer's vault. |
| **Secrets as constants/env, never DB** (`docs/12` §4.1) | All secrets via `MASSINGBILL_*` env vars; masked in the UI (last 4); never logged. Integration tokens encrypted at rest with AES-GCM (the osprey token-vault pattern). |
| **OpenAPI 3.1 as the client contract** | `docs/openapi/massingbill-v1.yaml`, same header/style as `massing-v1.yaml`, CI-validated, and a `sdk/python/` client mirroring `massing_data/client.py` (stdlib HTTP, dependency-light). |
| **Docs numbering** | `docs/01-…` … matching the massingcloud `docs/` convention, and a companion `docs/21-billing-service-spec.md` **to be added to the massingcloud repo** describing the bridge. |
| **CI shape** (`docs/12` §9 definition-of-done) | The Python analogue: ruff + `ruff format --check` + mypy **blocking**, pytest with a coverage floor on 3.11/3.12/3.13, pip-audit + SBOM, CodeQL, Semgrep. |

</details>

---

## 4. Data model

Money columns are `BIGINT` **integer cents** (§5). All ids are UUIDv7. Every table
carries `organization_id` and every query is org-scoped by a session guard, not by
developer discipline.

### 4.1 Core

- **Organization** — tenant. `name`, `slug`, `entitlement_cache`, `settings`.
  **Membership** (user × org × role). **Role** ∈ `owner | admin | pm | accountant |
  viewer | external_approver | sub_contact`.
- **Project** — `number`, `name`, `address`, `jurisdiction_state` (**drives
  retainage caps and waiver forms**), `is_public_work`, `is_residential`,
  `stories`, `owner_party_id`, `architect_party_id`, `status`.
- **PrimeContract** — `original_contract_sum_cents`, `execution_date`,
  `substantial_completion_date`, `billing_day_of_month`, `period_convention`
  (calendar month | 25th-to-24th | custom), `retainage_rule_id`,
  `stored_materials_allowed`, `offsite_stored_allowed`, `bonding_required`,
  `default_form_style` (`aia_style | house | custom`), `custom_form_template_id`.
- **RetainageRule** — `mode` ∈ `flat | split | variable_line | stepped`;
  `rate_work_bp`, `rate_stored_bp` (basis points); `reduction_threshold_bp`
  (e.g. 5000 = 50% complete), `reduced_rate_bp`; `release_policy`;
  `statutory_cap_bp` + `statute_citation` + `effective_from` (seeded per state,
  §0.4); `cap_enforcement` ∈ `warn | block`.

### 4.2 Schedule of values

- **ScheduleOfValues** — one per prime contract, `revision`, `status`
  (`draft | approved | superseded`), `approved_at`. Revisions are **immutable
  once an application references them**.
- **SovLine** — `item_no`, `description`, `csi_code`, `cost_code`,
  `scheduled_value_cents` (base), `co_adjustment_cents` (Σ from CO lines),
  `current_scheduled_value_cents` (= base + adj = **Column C**), `sort_order`,
  `is_co_line`, `source_change_order_id`, `unit`, `qty`, `unit_price_cents`
  (unit-price contracts), `retainage_rate_bp` (variable-retainage contracts),
  `group` (division/phase for subtotals), `is_general_conditions`,
  `is_allowance`, `allowance_balance_cents`.
- **CostCode** — org-level, seeded from CSI MasterFormat (shared YAML with
  scopemaker).

### 4.3 Change orders

- **PotentialChangeOrder** — `number`, `description`, `proposed_amount_cents`,
  `status` (`open | submitted | approved | rejected | void`), `pricing_backup`.
- **ChangeOrder** — `number`, `type` ∈ `owner_co | ccd | allowance_draw |
  unilateral`, `status`, `approved_date`, `amount_cents` (signed),
  `time_extension_days`, `applies_to_application_id` (the period whose "approved
  this month" box it lands in), `source_pco_id`.
- **ChangeOrderLine** — either creates a new `SovLine` or adjusts an existing one.
  A CO **never** silently rewrites a line's base value; it writes an adjustment
  row so the audit trail survives.

### 4.4 Applications (the heart)

- **Application** — `number` (sequential per contract, gapless), `period_start`,
  `period_end`, `application_date`, `status` ∈ `draft | in_review | submitted |
  certified | rejected | paid | void`, `form_style`, and the **frozen G702
  header**: `line1_original_sum`, `line2_net_co`, `line3_contract_sum_to_date`,
  `line4_completed_stored`, `line5a_retainage_work`, `line5b_retainage_stored`,
  `line5_total_retainage`, `line6_earned_less_retainage`,
  `line7_previous_certificates`, `line8_current_payment_due`,
  `line9_balance_to_finish` — all `_cents`. Plus `co_summary_prev_additions`,
  `co_summary_prev_deductions`, `co_summary_this_additions`,
  `co_summary_this_deductions`.
- **ApplicationLine** — one per SOV line per period, carrying **C, D, E, F, G, %,
  H, I** as stored cents. `D` and `line7` are **computed from the prior
  application and written once at period open** — never editable.
- **ApplicationSnapshot** — a full JSON freeze of the SOV, retainage rule, CO log
  and entitlement at submission, with a SHA-256. **A submitted application can be
  re-rendered byte-identically five years later even if the SOV has moved on.**
  This is the single most important durability decision in the schema.
- **PeriodLock** — enforces one open period per contract and blocks retroactive
  edits to a closed one.
- **Certification** — the architect/owner side: `amount_certified_cents`,
  `variance_cents` (vs Line 8), `reason`, `certified_by`, `certified_at`,
  `signature_id`.

### 4.5 Stored materials, subs, waivers, compliance

- **StoredMaterial** — `sov_line_id`, `description`, `location` ∈ `onsite |
  offsite | bonded_offsite`, `value_cents`, `invoice_ref`, `supplier`,
  `bond_ref`, `insurance_ref`, `installed_in_application_id` (the period it rolls
  from **F** into **E** — the roll is a transaction, never two independent edits,
  which is how double-billing happens in spreadsheets).
- **Subcontract** / **SubcontractLine** / **SubApplication** — the AP mirror of the
  prime side, so the GC can (a) collect sub billings on its own form and
  (b) roll them into the prime SOV. Sub-tier waiver requirements cascade.
- **WaiverTemplate** — `state`, `waiver_type` ∈ `conditional_progress |
  unconditional_progress | conditional_final | unconditional_final`,
  `statute_citation`, `effective_from`, `effective_to`, `body_template` (Jinja),
  `required_fields`, `notary_required`, `is_statutory`.
  **WaiverInstance** — bound to an application/sub-application, renders the
  template that was effective on `period_end`, tracks `status`
  (`requested | signed | notarized | rejected`), `signed_at`, `signature_id`.
- **ComplianceRequirement / ComplianceDoc** — COI, certified payroll, W-9,
  bonds, safety; `expires_on`, `blocks_payment` flag. A pay app can be **soft- or
  hard-blocked** on missing compliance — the behavior GCs actually pay for.

### 4.6 Audit

- **AuditEvent** — `actor`, `action`, `entity`, `before`, `after`, `at`,
  `request_id`, `ip`, `prev_hash`, `hash` (SHA-256 chain). Tamper-evident; a CLI
  command verifies the chain. Financial records are **never hard-deleted** —
  `void` is a state, not a `DELETE`.
- **TieoutReport** — the persisted result of §6 for a given application:
  rule id, severity, expected, actual, delta, pass/fail, plus an overall verdict
  and the snapshot hash it was computed against.

---

## 5. The money kernel (`services/money.py`)

Non-negotiable rules. A violation here is a defect regardless of test status.

1. **Storage is integer cents in `BIGINT`.** No `Float`. No `Numeric` on the money
   path. Cents in, cents out; `Decimal` only at the boundary.
2. **All arithmetic goes through `money.py`.** No `+`/`*` on money outside it.
   A ruff/grep CI check (`scripts/check_money_discipline.py`) fails the build if a
   money-suffixed attribute is arithmetic'd in a blueprint or template.
3. **Percentages are basis points (`int`, 1 bp = 0.01%).** `10%` is `1000`. There
   is no float percent anywhere.
4. **Rounding is `ROUND_HALF_UP` at exactly one place per computation**, and every
   rounded computation is followed by a **penny-reconciliation pass**: when a total
   is allocated across lines, the residual cents are distributed largest-remainder
   so that `Σ lines == header`, always, with the adjustment recorded.
5. **Retainage is computed per line and summed**, never computed on the header and
   pushed down. (The reverse is the #1 source of the one-cent disagreements that
   get pay apps rejected.)
6. **Signed money is explicit.** Deductive COs and credits are negative cents;
   nothing relies on an `is_credit` flag plus a positive magnitude.
7. **Currency is a first-class field** (`USD` default), even though v1.0 ships
   USD-only — retrofitting currency into a money engine is far worse than carrying
   an unused column.

API sketch:

```python
Cents = NewType("Cents", int)
Bp = NewType("Bp", int)


def apply_bp(amount: Cents, rate: Bp) -> Cents: ...  # ROUND_HALF_UP, once
def allocate(total: Cents, weights: Sequence[Cents]) -> list[Cents]: ...  # sums exactly
def to_display(amount: Cents) -> str: ...  # "$1,234,567.89"
def parse_money(raw: str) -> Cents: ...  # tolerant, locale-aware
```

---

## 6. The tie-out rule engine (`services/tieout.py`) — the differentiator

Every rule is a small pure function with an id, a human sentence, a severity, and
a citation to the G702/G703 definition it enforces. The engine runs on demand, on
every save, and **blocking** at submit. Output is a `TieoutReport` — rendered
in-app, appended to the PDF package as a "Reconciliation" page, and available at
`GET /api/massingbill/v1/applications/{id}/tieout`.

**Structural rules (severity: error — block submit)**

| Id | Assertion |
|---|---|
| `SOV-001` | `Σ SovLine.current_scheduled_value == line3_contract_sum_to_date` |
| `G702-003` | `line3 == line1 + line2` |
| `G702-002` | `line2 == Σ approved ChangeOrder.amount` |
| `G702-004` | `line4 == Σ ApplicationLine.G` |
| `G702-005` | `line5 == line5a + line5b` (flat/split) **or** `== Σ ApplicationLine.I` (variable) |
| `G702-006` | `line6 == line4 − line5` |
| `G702-007` | `line7 == prior_application.line6` (or `0` for App #1) |
| `G702-008` | `line8 == line6 − line7` |
| `G702-009` | `line9 == line3 − line6` |
| `G703-G`   | per line: `G == D + E + F` |
| `G703-H`   | per line: `H == C − G` |
| `G703-D`   | per line: `D == prior.D + prior.E` |
| `CO-SUM`   | CO summary box (prev ± this) nets to `line2` |
| `PENNY`    | `Σ line retainage == line5`; `Σ line C == line3` |

**Policy rules (severity: warning or error, configurable per contract)**

`OVERBILL` (`G > C` on any line) · `PCT>100` · `NEGATIVE-PERIOD` (negative `E`
without a deductive CO) · `RETAIN-CAP` (effective rate exceeds the state statutory
cap — cites SB 61 et al.) · `STORED-UNBACKED` (F with no invoice/bond/insurance
where the contract requires it) · `STORED-DOUBLE` (a material both in `F` and
already rolled into `E`) · `FRONTLOAD` (early-period % complete deviating from a
configured S-curve tolerance — the classic owner objection, surfaced *before* the
owner raises it) · `COMPLIANCE-BLOCK` (expired COI / missing certified payroll) ·
`WAIVER-MISSING` (prior-period unconditional waiver not on file) · `SEQUENCE`
(gap in application numbering) · `PERIOD-OVERLAP`.

**Informational:** cash-flow delta vs prior period, retainage held to date,
projected retainage release date under the governing statute, balance-to-finish
burn rate.

---

## 7. Testing strategy

Coverage floor **90% overall**, **100% on `services/money.py`,
`services/retainage.py`, `services/application.py`, `services/tieout.py`.**
Tests run offline, deterministically, on SQLite, with no network and no keys.

### 7.1 Layers

`tests/unit/` money, retainage modes, bp math, allocation · `tests/domain/` SOV
revisions, CO application, period open/close, stored-materials roll ·
`tests/tieout/` one test per rule id, pass **and** fail case each ·
`tests/renderers/` PDF (pypdf text extraction, as `scopemaker/tests/test_pdf_layout.py`
does), XLSX (formula strings **and** computed values), CSV/JSON schema ·
`tests/api/` contract tests against the OpenAPI doc, authz matrix, rate limits ·
`tests/web/` every route × every role (a 6-role × N-route matrix, table-driven) ·
`tests/integration/` Procore/QBO against recorded `responses` fixtures — **never**
live data · `tests/security/` CSP headers, CSRF, session fixation, IDOR probes
(org A cannot read org B — asserted on **every** resource type), password/TOTP
policy · `tests/migrations/` Alembic up→down→up on a seeded DB.

### 7.2 The golden project

`data/seed/demo/` defines one fixture that every layer reuses: a **$12,450,000
GMP project, 14 line items across 9 CSI divisions, 12 monthly applications**,
including — deliberately — every hard case:

- App 3: a **+$187,500 owner CO** adding two SOV lines mid-stream.
- App 4: **$340,000 of stored materials**, of which $95,000 is bonded off-site.
- App 5: those materials **roll from F into E** (the double-billing trap).
- App 6: a **−$42,000 deductive CO**.
- App 7: **retainage steps 10% → 5%** at 50% complete.
- App 8: **variable per-line retainage** (Column I) on two lines.
- App 9: the architect **certifies $60,000 less** than requested; App 10 must pick
  up `line7` from the *certified* amount, not the requested one. ← the subtle one
- App 11: an **allowance draw** and a **unit-price line** reconciliation.
- App 12: **final application** — retainage release, final waivers, consent of
  surety, zero balance to finish.

Every one of the 12 applications has a **hand-computed expected G702 header and
G703 grid checked into YAML**. If the engine and the YAML disagree, the build
fails. This fixture is also the demo data and the docs' worked example — one
source of truth for correctness, sales and documentation.

### 7.3 Property-based tests (hypothesis)

Over randomly generated contracts, SOVs and period sequences, assert the
invariants that must hold for *any* valid input: `Σ lines == header` under every
rounding path; `line9` monotonically decreases; the sum of all `line8` across all
periods equals `line3` at closeout; retainage never exceeds the cap; replaying a
period twice is idempotent.

### 7.4 CI (`.github/workflows/ci.yml`) — all jobs blocking

`lint` (ruff check + `ruff format --check` + mypy) · `test` (3.11/3.12/3.13, with
WeasyPrint native libs installed so PDF tests actually **run**, plus scopemaker's
"assert the pdf-marked tests didn't silently skip" guard) · `money-discipline`
(the grep gate from §5.2) · `openapi` (spec validates; routes match spec) ·
`security` (pip-audit + CycloneDX SBOM artifact) · `codeql` · `semgrep` ·
`migrations` (up/down/up) · `docker` (image builds, container healthchecks green).

Plus the three **standalone-integrity** jobs that keep §3's promise honest:

- **`offline`** — the full suite with outbound sockets blocked. Any accidental
  network call is a hard failure, not a slow test.
- **`no-adapters`** — deletes `services/*/massing_cloud.py`, `s3.py` and the
  `integrations/` tree, then re-runs the suite. It must stay green. This is the
  mechanical proof that the core does not depend on any adapter.
- **`import-linter`** — a contract forbidding any module outside
  `services/{entitlement,identity,storage}/massing_cloud.py` from importing a
  massing symbol, and forbidding `blueprints/` from importing `integrations/`
  directly. Coupling becomes a build failure rather than a code-review opinion.

---

## 8. Output layer

### 8.1 PDF (WeasyPrint, HTML/CSS templates)

Renderers: `aia_style` · `house` · `custom` (field-mapped onto a customer template).
The package assembles, in order: cover/transmittal → G702-style application →
G703-style continuation (paginated with carried-forward subtotals and a grand total
that ties) → change-order log → stored-materials schedule with backup index →
**tie-out reconciliation page** → lien waivers → compliance certificates.
Deterministic output (fixed timestamps in test mode) so PDFs are diffable.
Every page: project, application number, period, page x of y, and the §0.2 footer.

### 8.2 XLSX (openpyxl) — with live formulas

The G703 sheet ships **real formulas** (`=D5+E5+F5`, `=C5-G5`, `=G5/C5`) and the
G702 sheet references the G703 totals. An owner's accountant can click a cell and
see the arithmetic. Locked/protected structure, unlocked input cells, named
ranges, frozen panes, currency formats. This is the single most-requested export
in every competitor's review corpus, and the one the archived PHP repos got closest
to being right about.

### 8.3 CSV / JSON / API

Column order matches G703 exactly (§0.2 rule 4). JSON matches the OpenAPI schema
and is the payload for webhooks and the WordPress bridge.

---

## 9. Security & compliance posture

Mirrors `massingcoud/docs/10-security-compliance.md` and `docs/12` §4, in Python:

- **Auth:** argon2id; TOTP MFA with segno-rendered inline-SVG enrolment QR (keeps
  the strict `default-src 'self'` CSP intact — scopemaker's exact reasoning);
  session rotation on privilege change; account lockout with backoff.
- **AuthZ:** org-scoped session guard + role decorators; **IDOR-tested on every
  resource**; external approvers and sub contacts get scoped magic-link sessions
  with no dashboard access.
- **Web:** CSP `default-src 'self'`, HSTS, `X-Content-Type-Options`, referrer
  policy, `SameSite=Lax` + `Secure` + `HttpOnly` cookies, CSRF on every mutation,
  Flask-Limiter on auth/API/render, signed expiring download tokens.
- **Data:** integration tokens AES-GCM encrypted at rest; secrets from env only;
  uploads validated by extension **and** MIME (`finfo`) **and** content scan
  (`docs/12` C4/P2); random storage filenames; deny-all upload dir.
- **Records:** hash-chained audit log; immutable submitted applications; void-not-
  delete; documented retention; GDPR export/erase handlers for personal data
  (contacts, signatures) that do **not** destroy the financial record.
- **Supply chain:** pinned deps + `constraints.txt` (osprey pattern), pip-audit,
  SBOM, Dependabot, CodeQL, Semgrep, signed releases.
- **Ops:** `/healthz` + `/readyz`, structured JSON logs with request ids and no
  secrets, error tracking hook, backup/restore runbook, documented RPO/RTO.

### 9.1 E-signature — inherit the estate's own pattern, no vendor

**Finding: massing.cloud does not use an e-signature provider.** The estate was
searched for DocuSign / Dropbox Sign / Adobe Sign / PandaDoc / SignWell and there
is no integration, no adapter and no credential anywhere. The only mentions are
**sales copy**:

- `plugin/massing-subscriptions/includes/class-tiers.php:169` — the **Enterprise
  GC** tier lists *"AIA G702/G703 pay apps + e-signature"* as a feature bullet.
- `theme/massingcloud/front-page.php:24` — *"…AIA G702/G703 pay apps … e-signature
  and COBie turnover."*

So e-signature is **sold but not built**, and Massing Bill is the component that
has to make that bullet true. There is nothing to "integrate with."

The one **implemented** signature capture in the estate is
`C:\Server\wpemanager` (eManager): a vanilla pointer-event **canvas signature
pad** (`emanager/public/js/em-form.js` → `initSignaturePad`), submitted as a PNG
data URL, server-side validated as a genuine PNG and length-capped at ~300 KB
(`includes/class-em-api.php` → `sanitize_signature`, `SIGNATURE_MAX_LENGTH`),
stored with a timestamp and the signing user's identity. It is used today for
pre-task plans, T&M tickets, orientations and **lien-waiver routing**.

**Decision — v1.0 ports the eManager pattern and hardens it into a real
ESIGN/UETA evidence record.** No third-party vendor, no new secret, no per-envelope
cost, nothing that breaks the `default-src 'self'` CSP, and it stays consistent
with what massing already does in the field.

`Signature` record, captured at every signing point (pay-app certification,
approval steps, lien waivers, sub billings):

| Field | Purpose |
|---|---|
| `document_sha256` | Hash of the **exact rendered PDF bytes** that were on screen |
| `image_png` | The drawn mark (validated PNG, size-capped — eManager's rules) |
| `signer_user_id` / `signer_name` / `signer_email` / `signer_role` | Identity |
| `auth_method` | `session` · `magic_link` · `sso` · `totp_reconfirm` |
| `consent_text` + `consent_accepted_at` | The UETA intent-to-sign disclosure, versioned |
| `signed_at`, `ip`, `user_agent`, `request_id` | Attribution |
| `audit_event_id` | Link into the hash-chained audit log (§4.6) |

Rules: the signature binds a **hash, not a record** — if the document is
re-rendered and the hash changes, the signature is invalidated and re-signature is
required. A signed application is immutable (§4.4 snapshot). The evidence record
renders as a **Signature Certificate page** appended to the PDF package, which is
what an auditor or a court actually asks for.

**Honest limitation, carried into the docs verbatim** — eManager's own readme
states the estate's position and it applies here: canvas capture with timestamp
and identity *"is not a qualified e-signature service (ESIGN/eIDAS certification,
audit trails, certificates). For legally critical executions, use a dedicated
e-signature provider and attach the executed document URL to the record."* We
therefore ship, in P6:

- A `SignatureProvider` ABC with `InAppProvider` as the default and **only** v1.0
  implementation, so a DocuSign/Dropbox Sign adapter is a later drop-in rather
  than a refactor.
- An **"attach executed document"** path on every signable record — the eManager
  escape hatch — so a customer who runs a qualified provider externally can bind
  the executed PDF and its envelope id to the application without leaving Massing
  Bill.
- Per-state flags where a waiver requires **notarization** (§0.4): we render the
  notary block, mark the waiver `signed → awaiting_notarization`, and refuse to
  report it as complete. **Notarization is out of scope as a service** — we never
  claim to be an e-notary.

---

## 10. Integrations

| Target | Direction | Notes |
|---|---|---|
| **Procore** | both | Prime contracts + budget → SOV import; our app → **Payment Applications (Owner Invoices)**; **Requisitions (Subcontractor Invoices)** → sub billings. Lift `procore_client.py` / `procore_sync.py` from scopemaker; OAuth per [Procore docs](https://developers.procore.com/documentation/oauth-endpoints). |
| **QuickBooks Online** | out | AR invoice per certified application; retainage to a holding account; job costing by cost code. |
| **Sage 300 CRE / Intacct** | out | File-based/CSV export first (that is how these shops actually integrate), API adapter later. |
| **Excel/CSV import** | in | SOV import with column mapping + a dry-run diff before commit. |
| **massing.cloud** | both | Entitlements, SSO, vault, webhooks (§3). |
| **Textura / GCPay** | out | Export packages in their upload formats — meet subs where their GCs already are (Siteline's play). P7+. |

---

## 11. Phased roadmap

Each phase ships green CI and a tagged release. No phase starts before the prior
one's acceptance criteria pass.

**P0–P7 are the standalone product and are shippable as v1.0.** P8 is optional and
gated on a business decision that does not exist yet; nothing in P0–P7 depends on
it. Every phase's acceptance criteria are verified with the network blocked.

| Phase | Deliverable | Acceptance criteria |
|---|---|---|
| **P0 — Foundation** | Repo skeleton (`MassingCloud/massingbill`), app factory, config, extensions, the three adapter ABCs with only their standalone implementations, Docker, Alembic, CI (all jobs), MIT `LICENSE`, `README`/`CLAUDE.md`/`AGENTS.md`/`SECURITY.md`. | `git clone && docker compose up` serves `/healthz` **with no env file and no network**; CI green on 3.11/3.12/3.13; ruff + mypy zero findings. |
| **P1 — Money kernel** | `services/money.py`, cents/bp types, `allocate()`, the money-discipline CI gate, unit + hypothesis suites. | 100% coverage on `money.py`; property tests pass on 10k generated cases; gate fails a deliberately planted violation. |
| **P2 — Domain core** | Orgs, users, RBAC, **local** auth (argon2id + TOTP + invitations), `StandaloneProvider`, projects, prime contracts, SOV + revisions, CSI seed, audit log. | Full role × route matrix test green; IDOR probes fail closed; audit chain verifies via CLI; **no feature gate is reachable in standalone mode** (tested). |
| **P3 — The requisition engine** | Applications, period open/close/lock, ApplicationLine A–I, retainage (all 4 modes), change orders, stored materials + roll, snapshots. | **The 12-application golden project reproduces every hand-computed YAML value exactly.** |
| **P4 — Tie-out engine** | Every rule in §6, severities, `TieoutReport`, in-app view, blocking submit. | One passing + one failing test per rule id; App 9's certified-vs-requested `line7` case proven. |
| **P5 — Documents** | WeasyPrint `aia_style` + `house` renderers, XLSX with live formulas, CSV/JSON, package assembly, storage adapters, share tokens. | PDF text-extraction assertions pass in CI; XLSX formulas recompute to the engine's values in a round-trip test; legal footer non-removable (tested). |
| **P6 — Workflow, waivers, compliance, subs** | Approval workflow + e-signature evidence, 12 statutory waiver states + general (effective-dated YAML), compliance docs + payment blocks, subcontracts + sub pay apps + sub portal. | Waiver text golden tests per state/type/date; a pay app hard-blocks on an expired COI; sub-tier waiver cascade proven end-to-end; **re-rendering a signed document changes `document_sha256` and invalidates the signature** (tested); Signature Certificate page present in the package. |
| **P7a — API & webhooks** *(done)* | `/api/massingbill/v1` + OpenAPI 3.1 + Python SDK, API keys, webhooks + delivery log + retry (to **any** subscriber URL). | Spec/route parity test green **in both directions**; webhook HMAC verifies against massing's own published reference verifier; **suite still green with every optional adapter disabled**. |
| **P7b — Integration adapters** | Generic `OidcProvider`, `S3Storage`, Procore in/out, QBO, Sage export, SOV import. | Procore fixture round-trip passes; **suite still green with every optional adapter disabled**. |
| **P8 — Hardening & v1.0 release** | Load test, backup/restore drill, ops runbook, pen-test checklist, SBOM, docs site (GitHub Pages, as scopemaker), public demo instance, **v1.0.0 tag**. | Runbook executed end-to-end by someone who did not write it; restore drill verified; all §12 docs complete. **This is the standalone product, complete.** |
| **P9 — massing.cloud adapter** *(optional, post-1.0)* | `MassingCloudProvider` (validate + `effective()` merge + seat claim), massing.cloud as an `OidcProvider` config, `MassingVaultStorage`, massing registered as a webhook subscriber. | Fixture tests only; `on-hold` degrades to read-only; entitlement merge matches `docs/11` §6 reference output; **deleting `services/*/massing_cloud.py` leaves the suite green**. |
| **P10 — WordPress bridge** *(optional, deferred)* | `plugin/massing-billing` thin proxy, `docs/21-billing-service-spec.md` PR'd to massingcloud, proposed `class-tiers.php` flags. | A WordPress user signs in via the broker and reaches a billing project. Gated on the tier/pricing decision in §14. |

---

## 12. Documentation set (`docs/`, massingcloud numbering convention)

`01-getting-started` · `02-concepts-sov-and-applications` · `03-the-monthly-requisition`
(the worked 12-month example from §7.2) · `04-retainage-and-state-law` ·
`05-change-orders` · `06-stored-materials` · `07-lien-waivers-and-compliance` ·
`08-subcontractor-billing` · `09-forms-and-exports` · `10-security-compliance` ·
`11-rest-api` · `12-procore-and-erp` · `13-self-hosting` (the standalone install —
first-class, not an appendix) · `14-deployment` · `15-operations-runbook` ·
`16-tieout-rule-reference` (every rule id, its formula and its citation) ·
`17-optional-massing-cloud-integration` (explicitly marked optional) ·
`legal-forms-policy` · `openapi/massingbill-v1.yaml`.

Plus `CLAUDE.md` / `AGENTS.md` in the osprey style: architecture-do-not-drift,
golden rules (money discipline, snapshot immutability, no live-data tests),
where-things-live, and the exact command set.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| **AIA copyright** | §0.2 policy, enforced by a non-removable footer + a test that asserts it. Ship `house` and `custom` renderers so no customer depends on the AIA-shaped one. Legal review before public launch. |
| **A penny wrong destroys trust** | The entire §5 + §6 + §7 apparatus exists for this. Integer cents, one rounding site, penny reconciliation, 100% coverage, property tests, hand-computed golden fixture. |
| **State law changes** (SB 61 was *this year*) | Effective-dated YAML with citations; a quarterly review checklist in the runbook; the rule engine cites the statute in the warning text so a human can verify. |
| **Scope sprawl into full accounting** | §1.2 out-of-scope list is a contract. Integrations, not reimplementation. |
| **Procore API drift** | Recorded fixtures + a scheduled contract-check job; the integration degrades to manual import, never blocks billing. |
| **Two-language drift after folding into massing** | §3.4: the bridge is thin and stateless. No billing math ever exists in PHP. |
| **WeasyPrint native deps on Windows dev machines** | Docker for dev parity; PDF tests marked and gated in CI (scopemaker's proven approach); XLSX path has no native deps. |
| **Massing coupling creeps in anyway** — the classic failure of "standalone with an optional cloud" | Three enforced gates: (1) the default test suite runs with the **network blocked at the socket layer**; (2) a CI job **deletes `services/*/massing_cloud.py` and re-runs the suite** — it must stay green; (3) an import-linter rule forbids anything outside `services/{entitlement,identity,storage}/massing_cloud.py` from importing a massing symbol. Coupling becomes a build failure, not a code-review opinion. |
| **Standalone means no revenue signal** | Deliberate. MIT + self-hostable is the adoption path (osprey's thesis); monetization is the hosted service and the massing.cloud tier, both of which sit *outside* the engine. |

---

## 14. Decisions & open questions

### Decided

- **Repo home — `https://github.com/MassingCloud/massingbill`.** A new
  `MassingCloud` GitHub organization, distinct from `ibuilder` (where
  `scopemaker`, `massingcapture` and the four reference repos live). Consequences
  carried through this spec:
  - Package/dist namespace is `massingcloud-*` on PyPI, image
    `ghcr.io/massingcloud/massingbill`, docs site
    `massingcloud.github.io/massingbill`.
  - `.github/CODEOWNERS`, branch protection, Dependabot and the release signing
    key are org-level settings that must be established in **P0** — they do not
    inherit from `ibuilder`.
  - **Cross-org code reuse needs an explicit route.** P1/P6 lift
    `procore_client.py` / `procore_sync.py` / `models/procore.py` and the
    MasterFormat seed from `ibuilder/scopemaker`. Since the repos no longer share
    an owner, the plan is: **vendor the code into `massingbill` in P6 with
    provenance noted in the file header**, and only extract a shared
    `massingcloud-procore` package once both consumers are stable (P7+). Do not
    create a cross-org submodule.
  - This is also the natural home for the future `plugin/massing-billing` bridge
    and, over time, the rest of the massing.cloud estate.
- **Licence — MIT.** Matches `ibuilder/scopemaker`. Ship `LICENSE` (MIT, ©
  MassingCloud) in P0, set `license = { text = "MIT" }` and the
  `License :: OSI Approved :: MIT License` classifier in `pyproject.toml`, and
  state it in the README. Note the practical consequence: MIT does not compel
  hosted forks to publish changes, so the commercial moat is the massing.cloud
  entitlement/SSO layer and the hosted service — not the licence. The §0.2 AIA
  trademark/disclaimer obligations are independent of the licence and still apply.

- **Audience — the general contractor.** The paying customer and the licensed
  seat holder is the GC (project accountant, PM, controller). Everyone else in
  §1.1 is a *counterparty* the GC invites, not a customer:
  - **Owner / architect / owner's rep** — magic-link scoped session to review and
    certify. No dashboard, no seat consumed.
  - **Subcontractors** — the sub portal exists so the GC can *collect* sub
    billings, waivers and compliance docs. It is an AP intake surface, not a
    product sold to subs. Sub contacts get scoped magic-link sessions and consume
    no seats.
  - **Lender / auditor** — read-only share token onto a specific application's
    tie-out report and package.
  Consequences: seat counting and the massing.cloud `claim_seat` call cover **GC
  org members only**; pricing is per-GC-org; the sub portal ships **enabled** in
  v1.0 (it is a GC feature, not a separate product), but carries no sub-facing
  billing, onboarding or marketing surface. Sub-side self-service (a sub billing
  many GCs — the Siteline model) is explicitly **out of scope** and would be a
  separate product decision, not a v1.x feature.

- **E-signature — build it in-house, matching the estate.** massing.cloud has no
  e-signature provider; it only *sells* the feature in the Enterprise GC tier
  copy. v1.0 ports the eManager canvas-signature pattern and hardens it into a
  full ESIGN/UETA evidence record with a Signature Certificate page, behind a
  `SignatureProvider` ABC so a qualified provider can be added later, plus an
  "attach externally-executed document" escape hatch. Full detail and the honest
  limitation in **§9.1**.

- **Standalone is the product, not a mode.** Zero runtime dependency on
  massing.cloud. Every massing touchpoint is an optional adapter behind one of
  three ABCs, disabled by default, with CI gates that make coupling a build
  failure (§3, §13). P0–P8 ship a complete v1.0 with no massing involvement at
  all; P9/P10 are optional and post-1.0.

### Open — nothing blocking

One question remains. Both were **massing storefront decisions** rather than
engineering ones, and they gated **P9/P10 only** and can be answered any time in the next six
months. **P0 can start now.**

1. **Entitlement placement** — **DECIDED 2026-08-13: Option B.**
   GC billing is included in **Enterprise GC**, and available on **Commercial**
   as the `gc_billing` add-on ($249/mo). This honours the promise
   `class-tiers.php` has been making since before anything implemented it,
   without putting a $9,999/mo floor in front of every mid-size GC.

   Implemented in massingcloud `plugin/massing-subscriptions`: the six flags on
   the Enterprise tier, explicitly `false`/`0` on Commercial, and an add-on
   whose `grants` mirror the Enterprise set. Massing Bill reads that catalog
   rather than restating it, so nothing in this repo encodes the decision.

   Flags: `gc_billing`, `billing_projects`, `billing_apps_per_month`,
   `sub_tier_billing`, `esign`, `custom_forms`.

   Still to do on the storefront side: the WooCommerce product for the add-on,
   and a line of pricing copy.

2. **Hosted offering** *(gates nothing yet)*. Since the engine is MIT and
   self-hostable, is the commercial product (a) massing.cloud tiers only,
   (b) a separately hosted Massing Bill SaaS, or (c) both? This shapes the P8
   demo instance but not the code.
