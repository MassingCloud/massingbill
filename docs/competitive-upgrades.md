# Competitive review and upgrade plan

**Researched:** Oracle Textura, GCPay, Handle, Siteline — August 2026.
**Purpose:** decide what to add to the roadmap in `SPEC.md`, and what to
deliberately not add.

---

## 1. What each one actually is

### Oracle Textura Payment Management

The incumbent. Runs the whole draw: initiate a draw → sub pay applications →
markups and approvals → owner billing → lien-waiver and compliance collection →
enforcement → payment authorisation → **disbursement**. Notable capabilities the
others lack: **joint checks and split payments**, **direct payment to sub-tiers**,
and ACH disbursement with e-signed unconditional waivers exchanged against the
payment itself. Real-time waiver status across prime and sub-tier. Passed
$1 trillion in cumulative construction payments.
([Oracle](https://www.oracle.com/construction-engineering/textura-construction-payment-management/),
[docs](https://docs.oracle.com/cd/E97085_01/TPMhelp/en/North_America/10310246.htm))

**The vulnerability, and it is a big one.** Textura bills **subcontractors**
roughly **0.22% of contract value (capped near $3,750)**, plus about **$100 per
sub-sub or supplier contract** — for software their GC obliged them to use.
General contractors "rarely cover the cost", and subs have reported being told
only *after* their bid was accepted. The American Subcontractor Association
raised it formally with Textura on behalf of members. Reviewers also cite slow
support and overall cost.
([Levelset](https://www.levelset.com/blog/textura-friend-or-foe-subcontractors/),
[Levelset pricing Q&A](https://www.levelset.com/payment-help/question/textura-usage-charge/),
[G2](https://www.g2.com/products/oracle-textura-payment-management/reviews))

### GCPay

The direct competitor for our exact buyer. Built-in G702/G703, automated
submission and approval workflows, **automatic compliance-document collection
with expiry alerts**, conditional and unconditional waiver generation and
exchange, digital signatures, **Remote Online Notarization**, ACH batch payments
with status tracking, and out-of-the-box **ERP integrations with Sage 300 CRE,
Viewpoint Vista, CMiC and QuickBooks**.
([GCPay for GCs](https://ww3.gcpay.com/features/general-contractor-software/),
[ERP integrations](https://ww3.gcpay.com/features/erp-integrations/))

### Handle

The newest and the most interesting. $27M Series B; sells to material suppliers
and subcontractors rather than GCs, and to a *credit* audience (CFOs, VP Credit,
A/R managers) rather than a project audience. Products: **Lien & Notice
Management**, **Waiver Management** with **Custom Waiver Templates**,
**Automatic Deadlines**, **Full Service Research** (manual verification of
project parties against proprietary sources), **Job Sheets**, **Credit
Management**, **Online Payments**, and a **Construction Data Graph** underneath
it all. 50 states plus Canada, next-day eRecording.
([Handle](https://www.handle.com/), [waiver management](https://www.handle.com/payment-compliance/waiver-management/))

Four Handle features are worth stealing outright, because they are *validation*
features and validation is exactly what our engine is built to do:

1. **Automatic payment calculations** on the waiver — "partial payments,
   retainage, and exception scenarios" — with **overpayment and underpayment
   detection**.
2. **Waiver protection safeguards**: signature validation, and **amount
   verification across exchanges**.
3. **Data validation that catches mismatched data before documents are sent.**
4. **Automatic deadlines** driven by state statute.

### Siteline

Sells to subcontractors, and its whole wedge is *"generating the exact forms
your GCs require"* plus pushing into Textura and GCPay portals on the sub's
behalf. Confirms two things we already believe: **custom form mapping is a real
product**, and **the incumbent portals are something people pay to route around**.

---

## 2. Where Massing Bill already wins, and where it is behind

| | Massing Bill (planned) | Textura | GCPay | Handle |
|---|---|---|---|---|
| G702/G703 generation | ✅ P5 | ✅ | ✅ | — |
| **Provable tie-out with a reconciliation page** | ✅ **P4 — unique** | ✗ | ✗ | partial |
| **Self-hostable, source-available** | ✅ **unique** | ✗ | ✗ | ✗ |
| **Subcontractors pay nothing** | ✅ | ✗ (0.22%) | ✗ | ✗ |
| Lien waivers, 12 statutory states | ✅ P6 | ✅ | ✅ | ✅ 50 states |
| Compliance docs with expiry blocking | ✅ P6 | ✅ | ✅ | ✅ |
| Custom GC form mapping | ✅ P5 | ✗ | partial | ✅ |
| **Statutory deadline engine** | ✗ | partial | partial | ✅ **core** |
| **Remote online notarisation** | ✗ | ✗ | ✅ | ✗ |
| **Waiver amount verification across exchanges** | ✗ | partial | ✗ | ✅ |
| **Joint checks / sub-tier direct pay** | ✗ | ✅ **core** | partial | ✗ |
| ERP: Sage 300 CRE, Vista, CMiC | ✗ (QBO + CSV only) | ✅ | ✅ | ✅ |
| Portal export (Textura/GCPay upload) | ✗ | n/a | n/a | ✗ |
| Money movement (ACH) | ✗ **out of scope** | ✅ | ✅ | ✅ |

---

## 3. The strategic decision this research forces

**Subcontractors never pay.** Textura's sub fee is not a pricing detail; it is
the single most-complained-about thing in the category, and it is structural —
Textura's revenue depends on charging the party with the least leverage.

So this becomes a stated product principle, not a default:

> The general contractor is the customer. Subcontractors, suppliers, owners,
> architects, lenders and auditors use Massing Bill for free, forever, on any
> project a paying GC runs. There is no sub-side seat, no percentage of contract
> value, and no per-contract fee. A GC can tell a sub "you will not be charged
> for this" and be telling the truth.

That is defensible in a way a feature is not, because MIT licensing and
self-hosting make it structurally impossible for us to walk it back later.

**Added to `SPEC.md` §1.1 and §14.**

---

## 4. Proposed upgrades

Ranked by (value to a GC) ÷ (cost to us). Phase numbers refer to `SPEC.md` §11.

### Tier 1 — cheap, and they extend what we are already building

**U1. Waiver amount verification across exchanges** *(P6, ~2 days)*
Handle's "waiver protection safeguards", expressed as tie-out rules rather than
a separate product. A waiver states an amount; that amount must equal the
payment it releases, and the running total of waived amounts must never exceed
the running total paid. New rules: `WAIVER-AMOUNT` (waiver ≠ payment),
`WAIVER-OVER` (cumulative waived > cumulative paid), `WAIVER-THROUGH-DATE`
(through-date earlier than the period being released).
*Why it is cheap:* the rule engine already exists in P4; these are three more
rules and one join.
*Why it matters:* a conditional waiver for the wrong amount is how a contractor
accidentally releases lien rights they still needed.

**U2. Pre-send data validation ("job sheet" checks)** *(P6, ~2 days)*
Handle validates project data *before* documents go out. Ours becomes a
pre-send gate on the whole package: legal entity names present and consistent
across the pay app, the waivers and the compliance docs; a property address on
file; the owner and lender named where a state's notice requires it. Rules:
`PARTY-MISSING`, `PARTY-MISMATCH`, `ADDRESS-MISSING`.
*Why it matters:* a waiver naming the wrong legal entity is unenforceable, and
nobody notices until it is needed.

**U3. Overpayment and underpayment detection** *(P4, ~1 day)*
`PAY-VARIANCE`: the amount actually received against the amount certified,
with the running difference carried forward. Already half-built — we model
`Certification.amount_certified_cents` and its variance from line 8; this
extends it to recorded payments.

**U4. Automated reminders and follow-ups** *(P6, ~3 days)*
Both GCPay and Handle lead with this. Scheduled digests: waivers requested but
not returned, compliance documents expiring within 30 days, applications sitting
unapproved past the contract's review period, retainage becoming releasable
under the governing statute.
*Constraint:* email is an outbound side effect, so it ships behind an explicit
opt-in and a configured SMTP host — never a default that surprises a self-hoster.

### Tier 2 — real work, clear payoff

**U5. Statutory deadline engine** *(new phase P6.5, ~2 weeks)*
Handle's core product, and the thing our jurisdiction data already implies.
Effective-dated per-state rules computing preliminary-notice, notice-of-intent,
mechanics-lien and suit deadlines from first-furnishing, last-furnishing,
substantial-completion and notice-of-completion dates. Delivered as a
`DeadlineRule` seed alongside `retainage_rules/` and `waivers/`, each carrying a
statutory citation and an effective date, and surfaced as a dated obligation
list per project.
*Scope discipline:* we compute and warn. We do **not** file, serve, or record —
that is a regulated service business, and it would drag us into a different
company.

**U6. ERP integrations: Sage 300 CRE, Viewpoint Vista, CMiC** *(extends P7, ~2 weeks each)*
Table stakes against GCPay and Textura, and the single most common reason a GC
says no. Ship the file-based exchange first (which is how these shops actually
integrate), API adapters after. Behind the existing
`services/integrations/` seam, so none of it touches the core.

**U7. Portal export adapters — Textura and GCPay upload formats** *(P7, ~1 week)*
Siteline's play, inverted. A GC using Massing Bill still has owners who mandate
Textura; a sub using it still has GCs who mandate GCPay. Exporting into their
upload formats means adopting us never requires abandoning anything.
*Risk:* undocumented, unstable formats. Fixture-tested, degrades to manual
export, and never on the critical path.

**U8. Remote online notarisation** *(P6, ~1 week + vendor)*
GCPay's only unmatched feature. Several states require notarised waivers or
sworn statements; without it those projects cannot close a period in-app. Build
as a `NotarisationProvider` adapter (Proof/Notarize, NotaryCam) behind the same
seam pattern, **off by default** — it is a paid third-party service and a
self-hoster must not be forced into one.

### Tier 3 — model it, do not operate it

**U9. Joint checks and sub-tier payment tracking** *(P6, ~1 week)*
Textura's genuine differentiator, and it saves finance teams days. We model the
*instruction and the release*: a joint-check arrangement between a sub and its
supplier, whose waivers must both be satisfied before the line is releasable;
and sub-tier waiver cascade so a GC can see that a second-tier supplier has
released. We do **not** issue the check.
*This stays inside `SPEC.md` §1.2's exclusion of money movement*, which remains
the right call: ACH means money-transmitter licensing, and it is what turns a
software company into a payments company.

**U10. Payment recording and remittance tracking** *(P6, ~2 days)*
Not disbursement — just recording what was received, when, against which
application, so `PAY-VARIANCE` (U3) and retainage-release forecasting have real
data.

### Explicitly rejected

| Feature | Why not |
|---|---|
| **ACH disbursement / money movement** | Money-transmitter licensing in ~50 jurisdictions, and it changes what kind of company this is. `SPEC.md` §1.2 already excludes it; this research confirms it. |
| **Charging subcontractors** | §3 above. It is the competitor's weakness, not a revenue idea. |
| **Credit management / A/R scoring** (Handle) | A different buyer (VP Credit at a supplier) and a different product. Adjacent, not ours. |
| **Filing or recording liens** | A regulated service business with per-state requirements. We compute deadlines and warn; a law firm or a service files. |
| **"Full service research"** (Handle) | Human-in-the-loop verification of project parties. A services business with headcount, not software. |

---

## 5. Effect on the roadmap

No phase is removed and no phase is reordered. The additions land where the
machinery they need already exists.

| Phase | Added |
|---|---|
| **P4 — Tie-out engine** | U3 `PAY-VARIANCE` |
| **P5 — Documents** | *(no change; custom form mapping was already planned)* |
| **P6 — Workflow, waivers, compliance, subs** | U1 waiver amount verification · U2 pre-send validation · U4 reminders · U8 notarisation adapter · U9 joint-check modelling · U10 payment recording |
| **P6.5 — Deadline engine** *(new)* | U5 |
| **P7 — Integrations & API** | U6 Sage/Vista/CMiC · U7 portal export adapters |

Net addition to v1.0: roughly **three weeks**, almost all of it in P6 and the
new P6.5 — and about half of that is rule definitions and effective-dated YAML
rather than new machinery.

---

## 6. The positioning this research produces

> Textura and GCPay automate the paperwork and charge somebody for it — Textura
> charges the subcontractor. Handle automates the compliance calendar for
> suppliers. **None of them will show a general contractor a proof that the
> numbers tie.**
>
> Massing Bill produces the requisition *and* the reconciliation: every line of
> the G702, every column of the G703, every waiver amount and every statutory
> deadline, checked against each other and against the contract, with the
> failures named before the owner sees them. It runs on the GC's own
> infrastructure, and no subcontractor is ever charged to be billed by it.
