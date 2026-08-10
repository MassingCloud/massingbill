# Lien-waiver seed data

## The rule this directory exists to enforce

**Twelve states prescribe the exact wording of a lien waiver, and a waiver that
does not substantially conform to the statute can be unenforceable.** In several
of them — California, Texas, Georgia, Michigan — the form must be used *as
written*.

That makes waiver text the one place in this product where being approximately
right is worse than refusing to act. A pay application that is off by a cent
gets rejected and re-issued. A waiver built from plausible-but-wrong statutory
language releases lien rights the contractor still needed, and nobody finds out
until the money is gone.

## So statutory text ships unverified, and the engine refuses to render it

Each statutory state seeds with:

- the correct **citation** and **effective date**,
- the **waiver types** the statute prescribes,
- the **required fields** the form must carry,
- whether **notarisation** is required,
- and `verified: false` with an empty body.

`services/waivers.py` **refuses to render any statutory template whose body has
not been verified**, and says which statute to check. Replacing the body with
the verbatim text from the code, and flipping `verified` to `true`, is a
deliberate act by someone who has read the statute — not something that happens
because a seed file looked plausible.

The **general** form ships complete and verified. It is a conventional
conditional/unconditional waiver for the thirty-eight states with no prescribed
form, and it is what a project outside the twelve gets by default.

## The twelve states

| State | Citation | Notes |
|---|---|---|
| CA | Cal. Civ. Code §§ 8132–8138 | Four forms, must be used as written |
| TX | Tex. Prop. Code § 53.284 | Four forms, must be used as written |
| GA | O.C.G.A. § 44-14-366 | Must be used as written |
| MI | Mich. Comp. Laws § 570.1115 | Must be used as written |
| MS | Miss. Code § 85-7-419 | |
| AZ | A.R.S. § 33-1008 | Notarisation required |
| NV | Nev. Rev. Stat. § 108.2457 | |
| UT | Utah Code § 38-1a-802 | |
| WY | Wyo. Stat. § 29-10-101 | |
| MA | Mass. Gen. Laws ch. 254 § 32 | |
| FL | Fla. Stat. § 713.20 | Statute expressly permits other forms |
| MO | Mo. Rev. Stat. § 429.016 | Residential projects only |

Sources: [Levelset](https://www.levelset.com/blog/lien-waivers-12-states-with-required-forms/),
[GCPay](https://ww3.gcpay.com/blog/lien-waiver-requirements-by-state/).

## Effective dating

Every template carries `effective_from` and optional `effective_to`. A waiver
renders the text that was in force on the **period end date** of the application
it releases, not the text in force today. Retainage law moved in 2026 (see
`retainage_rules/`); waiver law moves too, and a waiver signed in 2025 must
still show the 2025 form.
