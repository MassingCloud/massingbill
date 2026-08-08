# Legal forms policy

**This is a binding engineering constraint, not guidance.** A change that
violates it is rejected in review regardless of test status.

## The constraint

AIA G702® *Application and Certificate for Payment* and G703® *Continuation
Sheet* are **copyrighted works of The American Institute of Architects**. A
purchaser receives a limited licence to reproduce roughly ten copies of a
*completed* form for one specific project; reproduction beyond that requires
written permission from the AIA.

Massing Bill therefore models a **numbering convention and an arithmetic** —
which is not what copyright protects — and never reproduces a form.

## What we may do

- Implement the G702 line structure (lines 1–9, the change-order summary) and the
  G703 column structure (A–I) as data and computation.
- Lay out our own documents that present those values, in our own typography,
  with our own wording.
- Name the line and column references in the UI, the API and the documentation,
  so a user knows that our "line 7" is their "line 7".
- Describe the output as **"AIA-style"** or **"following the AIA G702/G703 line
  structure"**.

## What we must never do

- Copy AIA form artwork, layout files, logos, or the AIA registered marks into
  the product.
- Copy AIA certification wording, notary blocks, or instruction-sheet text
  verbatim.
- Describe our output as an AIA document, an official form, or AIA-approved.
- Imply affiliation, endorsement or sponsorship.

## Required disclaimer

Every rendered document, and the application footer, carries:

> Prepared with Massing Bill. Format follows the AIA G702/G703 line structure.
> Massing Bill is not affiliated with, endorsed by, or sponsored by The American
> Institute of Architects. AIA®, G702® and G703® are registered trademarks of
> the AIA.

**The disclaimer is not configurable and cannot be removed from the `aia_style`
renderer.** A test asserts its presence; deleting that test is itself a
violation of this policy.

## The four renderers, and why there are four

No customer is ever forced through the AIA-shaped output.

| Renderer | What it is | Who it is for |
|---|---|---|
| `aia_style` | Our layout, G702/G703 line and column structure, disclaimer footer | The default; what most owners expect to receive |
| `house` | Clean-sheet Massing-branded form | Customers who want no resemblance at all |
| `custom` | Our fields mapped onto a GC- or owner-supplied template | Subcontractors billing a GC that mandates its own form |
| XLSX / CSV export | Column order matches G703 exactly | **The safe path** — a customer holding a real AIA licence populates their own official document |

The XLSX export exists specifically so that a licence holder never needs our
rendering at all. It is documented as such.

## If in doubt

Ask before shipping. The cost of a conversation is lower than the cost of a
cease-and-desist against a product whose whole promise is that it can be trusted
with a contractor's money.
