# Backlog

What is known to be missing at v1.3.1, in the order I would do it. Each item
says why it matters and what makes it non-obvious, because a backlog of task
names decays into a list nobody can prioritise six months later.

Verified against the code on 2026-08-13, not written from memory. Where an item
says "confirmed", I checked rather than assumed.

---

## 1. ~~Entitlements are built but never enforced~~ — **done 2026-08-15**

`massingbill/services/limits.py` is now the single place that asks, so "where
are we gated?" is a grep for `limits.require`. Five of the six flags have a call
site:

| Flag | Enforced at |
|---|---|
| `gc_billing` | project creation, `application.open_period` |
| `billing_projects` | project creation |
| `billing_apps_per_month` | `application.open_period` |
| `sub_tier_billing` | `subcontracts.create` |
| `esign` | `waivers.sign` |
| `custom_forms` | **nowhere — see below** |

`custom_forms` has no call site because **nothing can set
`PrimeContract.default_form_style`**. It is not on the contract form, and
`application.open_period` is the only reader. So the flag would gate an
operation that does not exist. It gets a gate when the field gets a UI; adding
one now would be a check nobody can trigger, which is worse than an absent one
because it reads as covered.

Three decisions worth knowing:

- **Standalone is unaffected**, and that is asserted first in `test_limits.py`.
  An absent limit means *unlimited*, not zero — the direction this has to fail
  in — so every gate is a no-op unless an operator opted into a provider that
  says otherwise. No licence, no phone-home, no kill switch still holds.
- **The provider is asked once per organization per request**, cached on `g` on
  top of the adapter's own TTL cache. Pinned by a test that counts calls.
- **Gating is at the write points, not a `before_request` hook.** Blunter would
  catch more, but it would also refuse things a lapsed customer must still be
  able to do — signing out, exporting their own data.

**Still open:** `esign` is checked at signing rather than at issue, deliberately
— a plan without it can still print the waiver and have it signed on paper.
Whether that is the commercial intent is a pricing question, not a code one.

## 1a. `created_at` is naive while everything newer is aware — *confirmed*

`TimestampMixin.created_at` / `updated_at` are plain `DateTime` with
`server_default=func.now()`, so they store whatever the database server's clock
says, with no offset. Every timestamp added since uses the `UtcDateTime`
decorator, whose docstring describes precisely the bug the old columns still
have.

Nothing is wrong today — a deployment whose database runs in UTC is consistent
either way, and that is the documented setup. But any code comparing
`created_at` against `utcnow()` has to remember to strip the tzinfo, which is
the kind of thing that gets remembered once. `application._opened_this_month`
does, with a comment saying why.

**What it needs:** switch the mixin to `UtcDateTime` and a migration that
rewrites the existing columns. Cheap in code, wide in blast radius — every
table has these two columns — so it wants its own change and its own backup.

## 2. No subcontractor portal

`Subcontract` and `SubApplication` exist as models and services, and a GC can
record a sub's billing. **A subcontractor cannot log in and submit one.**

That matters commercially rather than technically: Textura and GCPay are
sub-facing, and `docs/competitive-upgrades.md` commits to subs using this for
free, forever. Right now there is nothing for them to use.

**What it needs:** a scoped identity (a sub is not an org member), an invitation
flow, a narrow portal that can see only their own subcontract, and waiver
signing. The RBAC model already has an external-role concept to build on.

## 3. ~~Session fixation~~ — **done 2026-08-15**

`_finish_login` now clears the session before `login_user()`, so nothing that
existed before authentication survives it. One line, on all three paths —
password, MFA challenge, and the massing.cloud handoff, which share it.

Worth correcting the original entry, which overstated the exposure. Flask's
session is a **signed cookie**, so classic fixation does not apply: an attacker
who plants a cookie value cannot have it become authenticated, because the
server issues a *new* signed cookie carrying the user id and the attacker's copy
does not have it. The real gap was the other half — `login_user` adds to the
existing dict rather than replacing it, so any key an attacker got into the
pre-authentication session was carried into the authenticated one. That is
session poisoning rather than fixation, and clearing fixes it either way.

The ordering the old entry warned about held: every caller reads
`PENDING_MFA_KEY`/`PENDING_ORG_KEY` before calling, and passes what it needs as
an argument. A test pins the MFA challenge landing in the right organization so
that stays true.

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
