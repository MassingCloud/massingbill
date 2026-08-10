# Legal forms policy

**This is a binding engineering constraint, not guidance.** A change that
violates it is rejected in review regardless of test status.

**It is not legal advice.** It is the reasoning this project builds on, written
down so a lawyer can check it and so nobody has to reconstruct it later.

---

## 1. What is actually protected, and what is not

AIA G702® *Application and Certificate for Payment* and G703® *Continuation
Sheet* are published by The American Institute of Architects, which asserts
copyright in them and licenses their use. A purchaser gets a limited licence to
reproduce roughly ten copies of a *completed* form for one specific project.

That assertion is real, but it does not extend to everything on the page. Two
long-settled doctrines draw the line.

### Baker v. Selden (1879) — a system is not the book that describes it

Charles Selden copyrighted a book describing a bookkeeping system, illustrated
with ruled forms. Baker produced blank forms embodying the same system. The
Supreme Court held for Baker: **"blank account-books are not the subject of
copyright; and… the mere copyright of Selden's book did not confer upon him the
exclusive right to make and use account-books, ruled and arranged as designated
by him."** ([Baker v. Selden, 101 U.S. 99](https://en.wikipedia.org/wiki/Baker_v._Selden))

The arrangement of ruled columns that a *method of accounting* requires belongs
to the method, not to the publisher of a book about it.

### The blank forms rule — 37 C.F.R. § 202.1(c)

The Copyright Office codified the same principle. Not subject to copyright:

> **"Blank forms, such as time cards, graph paper, account books, diaries, bank
> checks, scorecards, address books, report forms, order forms and the like,
> which are designed for recording information and do not in themselves convey
> information."**
> ([eCFR](https://www.ecfr.gov/current/title-37/chapter-II/subchapter-A/part-202/section-202.1))

A continuation sheet is a ruled grid designed for recording information. Its
columns — scheduled value, work completed previously, work this period,
materials stored, total to date, balance to finish, retainage — are the columns
the *arithmetic of progress billing* requires. So is a nine-line summary that
adds two figures, subtracts a third, and carries a fourth forward.

### The limit, stated honestly

The doctrine is not unlimited. Where a form **conveys information** rather than
merely recording it, or where forms and instructions together form an integrated
original work, thin copyright can attach to the original elements. Courts have
split on the boundary, and this is exactly why the policy below is drawn
conservatively rather than to the edge of what might be defensible.

---

## 2. The line this project draws

**We implement the structure and the arithmetic. We write every word ourselves.**

### Permitted

- The G702 line structure (lines 1–9, the change-order summary box) and the G703
  column structure (A–I) as data and computation.
- Our own layout, typography and page furniture presenting those values.
- Naming the line and column references in the UI, API and documentation, so a
  user knows our "line 7" is their "line 7". This is nominative use of a
  reference, not reproduction of a work.
- Describing output as **"AIA-style"** or **"following the AIA G702/G703 line
  structure"** — factual, and how every competitor describes theirs.

### Never

- Copying AIA form artwork, layout files, logos or registered marks.
- Copying **certification wording** — the contractor's sworn statement, the
  architect's certificate, the notary block. This is the strongest part of the
  AIA's position: it is expressive prose, not a ruled grid, and it is the one
  element the blank-forms doctrine plainly does not reach. **Every certification
  paragraph in this product is written from scratch.**
- Copying instruction-sheet text.
- Describing our output as an AIA document, an official form, or AIA-approved.
- Implying affiliation, endorsement or sponsorship.

The practical test for a contributor: *would this sentence exist if the AIA had
never published anything?* Column headings and line numbers, yes — they follow
from the arithmetic. A paragraph of certification prose, no. Write your own.

---

## 3. Required disclaimer

Every rendered document, and the application footer, carries:

> Prepared with Massing Bill. Format follows the AIA G702/G703 line structure.
> Massing Bill is not affiliated with, endorsed by, or sponsored by The American
> Institute of Architects. AIA®, G702® and G703® are registered trademarks of
> the AIA.

**Not configurable, and not removable from any renderer.** A test asserts its
presence in every output format; deleting that test is itself a violation of
this policy.

The trademark half matters independently of copyright. "AIA", "G702" and "G703"
are registered marks, and the risk there is not copying — it is *confusion*
about who produced the document. The disclaimer answers that directly.

---

## 4. The four renderers, and why there are four

No customer is ever forced through the AIA-shaped output.

| Renderer | What it is | Who it is for |
|---|---|---|
| `aia_style` | Our layout and our words, carrying the G702/G703 line and column structure | The default; what most owners expect to receive |
| `house` | Clean-sheet Massing-branded form | Customers who want no resemblance at all |
| `custom` | Our fields mapped onto a GC- or owner-supplied template | Subcontractors billing a GC that mandates its own form |
| XLSX / CSV export | Column order matches G703 exactly | **The safe path** — a licence holder populates their own official document |

The XLSX export exists precisely so a licence holder never needs our rendering
at all. It is documented as such, and its column order is pinned by a test.

---

## 5. Why this is the right posture commercially, not just legally

Every serious competitor ships "AIA-style" output with a disclaimer. Nobody
licenses the forms to resell them, because the licence is per-project and
per-purchaser and does not contemplate a software vendor distributing them.

The AIA sells documents. We sell a proof that the numbers tie. Those are
different products, and the second one does not require the first.

---

## 6. If in doubt

Ask before shipping. The cost of a conversation is lower than the cost of a
cease-and-desist against a product whose whole promise is that it can be trusted
with a contractor's money.

Before any public launch, have counsel review: this document, one rendered
`aia_style` PDF, and the README's description of the output.
