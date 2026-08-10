# Folding Massing Bill into massing.cloud

What is already true, what is left, and why the remaining work is small.

## The shape of the problem

massing.cloud is **WordPress + WooCommerce, PHP ≥ 7.4** (`massing/storefront`),
serving its API from `https://massing.cloud/wp-json`. Massing Bill is Flask on
WSGI. Those two never share a process, which means "pulled into massing" can only
ever mean **WordPress talking HTTP to a billing service** — not importing a
Python package.

That is why there is no FastAPI here, and why adding it would buy nothing: PHP
cannot tell what is behind the socket. What matters is the wire contract, and
that has been fixed since P0.

## Why this is not speculative work

`plugin/massing-subscriptions/includes/class-tiers.php:169` already sells:

> `AIA G702/G703 pay apps + e-signature` — Enterprise GC tier

and `theme/massingcloud/front-page.php` repeats the claim. Nothing in the
storefront implements it. This engine is what makes that line true.

## What is already done

### The five conventions (SPEC.md §3.1)

Honoured unconditionally, including in a fully standalone install, because they
are free now and expensive to retrofit.

| Convention | Where it lives |
|---|---|
| REST namespace and envelope | `/api/massingbill/v1`, resource nouns, `{"data": …, "meta": …}` |
| Error-code table | `errors.py` — 401 bad secret, 403 out of scope, 404 not found, 409 refused, 429 limited |
| `Authorization: Bearer` / `X-Api-Key` | `blueprints/api.py::_presented_token` — both accepted, as massing accepts both |
| Hex HMAC-SHA256 in `X-Massing-Signature` | `services/webhooks.py::sign` |
| Entitlement object shape | `services/entitlement/base.py` — `tier`, `entitled`, `limits{}`, `seats{}`, `expires_at` |

The signature is checked in CI against massing's **own published verifier**,
copied verbatim from `docs/05-rest-api.md` rather than imported, so that if
either side changes its scheme the build fails instead of both drifting together
(`tests/test_webhooks.py`).

### The three adapter seams

Abstract, with a standalone default, and named in `optional.py` as modules the
core may never import. CI deletes them and re-runs the suite to prove it.

| Seam | Standalone default | massing implementation (P9) |
|---|---|---|
| `EntitlementProvider` | `StandaloneProvider` — unlimited | `MassingCloudProvider` |
| `IdentityProvider` | `LocalPasswordProvider` — argon2id + TOTP | massing as an `OidcProvider` |
| `StorageBackend` | `LocalStorage` | `MassingVaultStorage` |

## What is left

### P7b — integration adapters

`OidcProvider`, `S3Storage`, and the Procore / QuickBooks / Sage exports. None
of these gate massing integration; they are separate customer asks.

### P9 — the massing adapter

Four files against interfaces that already exist:

1. `services/entitlement/massing_cloud.py` — validate, `effective()` merge, seat
   claim. The entitlement object it returns already has massing's field names,
   so nothing downstream changes.
2. massing.cloud registered as one more entry in `MASSINGBILL_OIDC_PROVIDERS`.
3. `services/storage/massing_vault.py` — signed pointer.
4. A `WebhookSubscription` row pointing at massing. **No code at all** — massing
   becomes a subscriber exactly like a customer's ERP, verifying with the code
   already in its own docs.

That last point is the payoff of the standalone discipline: the event path needs
zero massing-specific work.

### P10 — the WordPress bridge

`plugin/massing-billing`, a thin PHP proxy. It holds an API key, calls
`/api/massingbill/v1`, and renders. Gated on pricing, not on engineering.

## The decision that gates P9

Not an engineering decision (SPEC.md §14):

- **Option A** — bundle `gc_billing` into Enterprise GC only. No marketing
  change, but a $9,999/mo floor excludes every mid-size GC.
- **Option B** *(recommended)* — Enterprise GC **and** `addon:gc_billing` on
  Commercial. Honours the existing promise, opens the mid-market, costs one
  entry in `class-tiers.php` and one line of copy.

Either way the flags are already named: `gc_billing`, `billing_projects`,
`billing_apps_per_month`, `sub_tier_billing`, `esign`, `custom_forms`.

## What must not happen

The standalone core stays standalone. Three CI jobs enforce it and none of them
may be relaxed to make integration easier:

1. **`offline`** — the suite runs with outbound sockets refused.
2. **`no-adapters`** — the adapter modules are deleted and the suite re-runs.
3. **`imports`** — an import contract forbids the core from importing them.

If folding into massing ever requires weakening one of these, the design has
gone wrong and the fix is on the adapter side, not the gate.
