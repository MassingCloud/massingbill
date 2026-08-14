# Backlog

What is known to be missing at v1.3.1, in the order I would do it. Each item
says why it matters and what makes it non-obvious, because a backlog of task
names decays into a list nobody can prioritise six months later.

Verified against the code on 2026-08-13, not written from memory. Where an item
says "confirmed", I checked rather than assumed.

---

## 1. Entitlements are built but never enforced — *confirmed*

`app.extensions["massingbill_entitlement"]` is constructed at startup and
**nothing ever calls it**. No `.effective()`, no `.allows()`, no `.within()`
anywhere outside the adapter's own tests.

So `gc_billing`, `billing_projects`, `billing_apps_per_month`, `sub_tier_billing`,
`esign` and `custom_forms` currently decide nothing. The whole P9 adapter — the
outage handling, the seat claims, the grace window — feeds a system with no
consumers.

This is the largest functional gap in the product and the least visible, because
everything about it *looks* finished.

**What it needs:** decide where each limit is checked (project creation, period
open, waiver issue, form-style selection), then a decorator or service call at
each. Read-only degradation is already modelled in `Entitlement.read_only`.

**Unblocked as of 2026-08-13:** the tier decision is made and the flags exist,
so there is now a real answer to enforce rather than an empty catalog.

**Watch for:** the entitlement must not be consulted on every request without
thought — see the negative-caching note in `massing_cloud.py`. And a standalone
install must stay unlimited, which `StandaloneProvider` already does.

## 2. No subcontractor portal

`Subcontract` and `SubApplication` exist as models and services, and a GC can
record a sub's billing. **A subcontractor cannot log in and submit one.**

That matters commercially rather than technically: Textura and GCPay are
sub-facing, and `docs/competitive-upgrades.md` commits to subs using this for
free, forever. Right now there is nothing for them to use.

**What it needs:** a scoped identity (a sub is not an org member), an invitation
flow, a narrow portal that can see only their own subcontract, and waiver
signing. The RBAC model already has an external-role concept to build on.

## 3. Session fixation — *confirmed*

`login_user()` is called without rotating the session identifier, on **both**
the password path and the handoff. An attacker who can set a victim's session
cookie before sign-in keeps a valid session afterwards.

Pre-existing rather than introduced by the bridge, and lower severity than it
sounds given `SameSite=Lax` and `Secure` cookies — but it is a one-line fix in
`_finish_login` and there is no argument for leaving it.

**What it needs:** clear and re-issue the session on privilege change, taking
care not to lose `PENDING_MFA_KEY`/`PENDING_ORG_KEY` at the wrong moment.

## 4. No load test, no external penetration test

Stated as a known limit in the 1.0.0 changelog and still true. The security
posture in `docs/runbook.md` §7 describes what was *built*, not what an
independent party has confirmed.

**What it needs:** the load test is straightforward and worth doing first —
concurrent period opens against one contract will exercise the SOV revision
locking, which is the place a race would be expensive. The pen test is a
purchase, not a task.

## 5. ERP integrations are file-based only

Sage 300 CRE, Viewpoint Vista, QuickBooks, Textura and GCPay all get a CSV.
That was the deliberate first step (`docs/competitive-upgrades.md` U6/U7: file
exchange is how these shops actually integrate), but it is half the story.

Two distinct pieces of work:

- **API adapters** for Sage/Vista/CMiC. Real integration work, one at a time,
  behind the existing `services/integrations/` seam.
- **The portal upload formats.** The current Textura and GCPay exports are a
  plain G703-ordered sheet, *not* their actual specs — which are undocumented
  and change without notice. Fixture-test them and keep them off the critical
  path, as the competitive doc already says.

## 6. Remote online notarisation (U8)

Several states require notarised waivers or sworn statements. Without it, those
projects cannot close a period entirely in-app. GCPay's one unmatched feature.

**What it needs:** a `NotarisationProvider` adapter behind the same seam as the
other three (Proof/Notarize, NotaryCam), plus a vendor decision. The waiver
model already carries `notary_reference`.

## 7. `amr`-based MFA skip, when there is something to trust

Today a user with TOTP enrolled is challenged after a handoff, because the
assertion asserts nothing about how they authenticated. That is correct now and
will become friction if massing.cloud adds its own MFA.

**What it needs:** massing.cloud asserting `amr` ([RFC 8176][8176]) or `acr`,
and this side accepting it in lieu of the local challenge — under an explicit
config flag, because it is a relaxation. See `docs/runbook.md` §5a.

[8176]: https://www.rfc-editor.org/rfc/rfc8176.html

---

## Not engineering — these need a decision or a permission

- **Branch protection on `main`.** One `gh api` call; blocked for me by the
  permission classifier. `main` is currently force-pushable and deletable.
- **`@MassingCloud/maintainers`** — needs `admin:org`.
- ~~**The tier decision**~~ — **decided 2026-08-13: Option B.** GC billing is
  in Enterprise GC and available on Commercial as the `gc_billing` add-on. The
  six flags are live in massingcloud's `class-tiers.php` and `class-addons.php`,
  so **item 1 above is now unblocked** — there is something real to enforce.
  Still outstanding on the storefront: the WooCommerce product for the add-on,
  and a line of pricing copy.
- **48 statutory waiver forms and every deadline rule.** They refuse rather
  than guess, by design. `massingbill statutory export` makes filling them a
  spreadsheet; the words must come from someone reading the statute.

---

## Deliberately not doing

Recorded so nobody re-opens them as oversights. Reasoning in
`docs/competitive-upgrades.md`.

- **Moving money.** ACH, cards, disbursement. Money transmission means
  licensing in fifty jurisdictions.
- **Charging subcontractors.** The general contractor is the customer.
- **Filing, serving or recording liens.** We compute deadlines and warn.
- **Inventing statutory text.** See above, and `docs/legal-forms-policy.md`.
