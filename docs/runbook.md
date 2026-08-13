# Operations runbook

For whoever is on the other end of the phone when a pay application is due
tomorrow. Written to be followed by someone who did not build this.

Every command assumes you are in the deployment directory with `docker compose`
available. Substitute `massingbill` for `docker compose exec app massingbill` if
you run it directly.

---

## 0. Before you touch anything

```bash
docker compose exec app massingbill check
```

Prints the resolved environment, database URL and adapter selection without
serving a request. If this fails, the problem is configuration, not data, and
nothing below will help until it passes.

## 1. First install

```bash
docker compose up -d
docker compose exec app massingbill create-admin --email you@example.com --organization "Your Company"
```

Two secrets are **required** and the container refuses to start without either:

| Variable | What it protects | Rotating it |
|---|---|---|
| `MASSINGBILL_SECRET_KEY` | Session cookies | Signs everyone out. Safe. |
| `MASSINGBILL_ENCRYPTION_KEY` | TOTP seeds, webhook secrets, integration tokens at rest | **Not safe.** See §6. |

Generate each with `massingbill gen-secret`. They are separate on purpose:
rotating a session key must not lock every user out of two-factor.

## 2. Backups

The database is the whole product. Documents can be re-rendered from it; it
cannot be reconstructed from them.

**Postgres**

```bash
docker compose exec -T db pg_dump -U massingbill --format=custom massingbill > backup-$(date +%F).dump
```

**SQLite** — do not copy the file while the app is running; a copy taken
mid-write is a corrupt database that restores cleanly and fails later.

```bash
docker compose exec app sqlite3 /data/massingbill.sqlite ".backup '/data/backup.sqlite'"
docker compose cp app:/data/backup.sqlite ./backup-$(date +%F).sqlite
```

Back up `MASSINGBILL_ENCRYPTION_KEY` **separately from the database**, somewhere
the database backup does not reach. A backup encrypted with a key stored inside
it is not a backup.

### Verify the backup, not the backup job

A backup nobody has restored is a hypothesis.

```bash
# Restore into a scratch database and check the chain end to end.
createdb massingbill_restoretest
pg_restore -d massingbill_restoretest backup-2026-08-09.dump
MASSINGBILL_DATABASE_URL=postgresql://…/massingbill_restoretest massingbill audit verify
```

`audit verify` walks every organization's hash chain. It exits non-zero on a
break, so it works from cron. **A restored database whose audit chain verifies
is a restored database.** One that does not is corrupt, truncated, or was taken
mid-write — and you want to learn that now.

## 3. Upgrades

```bash
docker compose pull
docker compose exec app massingbill check      # config still parses?
docker compose exec app flask db upgrade       # migrate
docker compose up -d
```

Migrations are tested up, down and up again in CI, so a failed upgrade can be
rolled back with `flask db downgrade`. Take a backup first anyway.

## 4. Routine jobs

Both exit non-zero when there is something to look at, so cron reports them.

```bash
# Deliver queued webhooks. Nothing is sent inside a request.
*/5 * * * *  docker compose exec -T app massingbill webhooks drain

# Verify every audit chain.
17 3 * * *   docker compose exec -T app massingbill audit verify

# Which open periods will not submit, days before the deadline rather than at it.
0 7 * * 1    docker compose exec -T app massingbill tieout sweep --organization <id>

# Only if the massing.cloud bridge is configured. Spent sign-in records are
# what make a handoff link single-use; nothing else deletes them, so without
# this the table grows by one row per sign-in forever.
40 3 * * *   docker compose exec -T app massingbill handoff prune
```

## 5. When something is wrong

### An application will not submit

By design: it does not tie out. The reconciliation panel names every failing
rule with its citation. `massingbill tieout sweep` gives the same answer from
the command line. **Do not look for an override — there isn't one, on purpose.**
Fix the number, or void the period and open it again.

### The audit chain fails verification

```bash
docker compose exec app massingbill audit verify
```

Reports the first sequence number where the chain breaks. This means rows were
edited outside the application — restore from backup and find out who has
direct database access. The chain is hash-linked, so a break tells you *where*
the tampering started, not merely that it happened.

### A signed waiver shows as not intact

The rendered document no longer matches what was signed. The signature covers
exact bytes; if they changed, the signature cannot be relied on. Re-issue the
waiver and have it signed again. Do not "fix" the document — that is the failure
being reported, not a display bug.

### Webhooks are not arriving

```bash
docker compose exec app massingbill webhooks drain
```

Deliveries retry with exponential backoff and are abandoned after six attempts;
a subscription is disabled after twenty consecutive failures. The delivery log
records what was sent, when, and the response — check it before believing the
receiving end. "Did you send it?" is the first question in every integration
dispute and the log is the answer.

### PDF rendering fails

The container carries the WeasyPrint native stack. A hand-rolled install may
not: `GET /api/massingbill/v1/status` lists the formats this deployment can
actually produce. XLSX, CSV and JSON do not need it.

## 5a. The massing.cloud bridge (optional)

Off unless configured. A standalone install needs none of this and the sign-in
endpoint returns **404** until a shared secret is set — deliberately, so an
unconfigured deployment does not advertise something it cannot honour.

Three variables, and the secret must match the one in the WordPress plugin:

| Variable | Meaning |
|---|---|
| `MASSINGBILL_MASSING_BASE_URL` | Usually `https://massing.cloud` |
| `MASSINGBILL_MASSING_SHARED_SECRET` | Signs the sign-in handoff. Must match `MASSING_BILLING_SHARED_SECRET` in `wp-config.php`. |
| `MASSINGBILL_MASSING_API_KEY` | Read entitlements and claim seats. The same `mcds_…` key as every other Massing service. |

Install `massingbill[massing]` for the entitlement and vault adapters. Without
the extra they are simply absent, which is a supported state rather than a
broken one.

### When somebody cannot sign in through the bridge

Every refusal shows the visitor the same message on purpose — telling a caller
whether the signature failed, the link expired, or the account does not exist is
free reconnaissance. **The reason is in the log**, at `WARNING`:

```bash
docker compose logs app | grep "massing handoff refused"
```

The usual causes, in the order they occur:

- **`assertion rejected: bad signature`** — the two secrets differ.
- **`assertion rejected: expired`** or `older than a redirect` — a link is good
  for sixty seconds. If this happens routinely, the two servers' clocks disagree
  by more than the 30-second tolerance.
- **`has already been used`** — someone reloaded the handoff URL, or a browser
  prefetched it. Links are single-use; go back to massing.cloud and click again.
- **`no account for …`** — deliberate. The bridge never creates accounts. Invite
  the person in Massing Bill first, then the link works.
- **`… is not a member of …`** — the account exists but is in a different
  organization from the one the assertion names.

### If the endpoint 404s

The secret is not set. Check `massingbill check`, and remember an empty string
counts as unset.

## 6. Rotating the encryption key

There is no automatic rotation, deliberately — a half-rotated database is worse
than an old key.

1. Take a backup and verify it restores (§2).
2. Stop the application.
3. Decrypt and re-encrypt every `secret_encrypted` column with a script that
   holds both keys.
4. Swap the key and restart.

If the key is **lost**: stored TOTP seeds and webhook secrets are unrecoverable.
Users must re-enrol two-factor and webhook subscriptions must be re-created.
Financial data is unaffected — it is not encrypted at rest, only secrets are.

## 7. Security posture

- Sessions are cookie-based, `Secure`, `HttpOnly`, `SameSite=Lax`, and CSRF is
  enforced on every form. The API is exempt because a Bearer key carries no
  ambient authority.
- Passwords are argon2id. API keys are 256-bit random, stored only as SHA-256
  digests — a database disclosure yields no usable keys.
- A strict `default-src 'self'` CSP with no external origin. The TOTP QR code is
  an inline SVG rather than a third-party chart URL, which is what keeps that
  true.
- Rate limits are per API key rather than per address, so one busy customer
  cannot throttle everyone behind the same NAT.
- Cross-tenant reads answer `404`, never `403`: "this exists but is not yours"
  confirms another contractor's id is real.

Report a vulnerability per [`SECURITY.md`](../SECURITY.md).

## 8. What this product will not do

Worth knowing before someone asks you to make it.

- **It does not move money.** No ACH, no card, no disbursement. It records what
  was received. Money movement means money-transmitter licensing in fifty
  jurisdictions.
- **It does not file, serve or record liens.** It computes deadlines and warns.
- **It does not invent statutory text.** Prescribed waiver forms and deadline
  day counts ship empty and refuse until somebody reads the statute and enters
  them. If a screen refuses, that is the feature working.
- **It does not charge subcontractors.** The general contractor is the customer.
