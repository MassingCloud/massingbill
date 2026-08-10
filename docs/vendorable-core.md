# `massingbill/core/` — the calculation, with nothing attached

**Zero runtime dependencies. Standard library only.**
[`tests/test_architecture.py`](../tests/test_architecture.py) fails the build
when that stops being true.

Conforms to modelmaker's
`docs/internal/vendorable-core-standard.md`.

## What is in it

| Module | What it does |
|---|---|
| `core/money.py` | Integer cents, basis points, one rounding site, largest-remainder allocation |
| `core/retainage.py` | Withholding per line in four modes, summed to the header |
| `core/requisition.py` | The G702 header and G703 continuation sheet, from plain values |
| `core/enums.py` | The vocabulary the arithmetic branches on |

Everything else — `models/`, `services/`, `blueprints/` — is an adapter over
these. Persistence, workflow and transport call the core; the core knows about
none of them.

## Using it without a database

```python
from massingbill.core import LineEntry, RetainageSpec, cents, compute_application

app = compute_application(
    [
        LineEntry("001", cents(100_000_00), this_period=cents(40_000_00)),
        LineEntry("002", cents(250_000_00), this_period=cents(60_000_00), stored=cents(20_000_00)),
    ],
    original_contract_sum=cents(350_000_00),
    net_change_orders=cents(10_000_00),
    previous_certificates=cents(0),
    retainage=RetainageSpec(rate_work_bp=500, rate_stored_bp=500),
)

app.line8_current_payment_due  # int, cents
app.ties_out()  # the arithmetic identities hold
```

No session, no app context, no config. `original_contract_sum` and
`net_change_orders` are separate arguments because the G702 reports them on
separate lines and an owner reconciles them separately — collapsing them loses
what line 2 exists to show.

## The rules for anything added here

1. **Standard library only.** If it needs a third-party package, it belongs in
   `services/` or behind an optional adapter.
2. **No `massingbill.*` imports except `massingbill.core.*`.** If the core
   genuinely needs an enum, move the enum here and re-export it from its old
   home, so there is still exactly one definition. `RetainageMode` is the worked
   example: defined in `core/enums.py`, re-exported by `models/project.py`.
3. **No session, request or config object in a signature.** That is the test for
   whether something is calculation or plumbing.
4. **Integer cents.** Never a float, never `Decimal` on the money path.

`massingbill/__init__.py` holds only the version and a PEP 562 lazy handle on
`create_app`, because importing any submodule runs it — a top-level
`from flask import Flask` there would make the core drag Flask along.

## The tests that travel

`core/tests/test_mb_*.py` ship with the core and are **not** pytest: plain
`assert`, a `__main__` runner, standard library only.

```bash
python massingbill/core/tests/test_mb_requisition.py
```

Two deliberate choices, both from defects found adopting massingplan:

- **Stdlib-runnable**, because a consuming repo may have no pytest. Ten vendored
  suites failed there with `ModuleNotFoundError: No module named 'pytest'`. The
  upstream suite in `tests/` still uses pytest; this is the copy that travels.
- **`test_mb_` prefixed**, because a consumer with hundreds of flat test modules
  will resolve a bare `test_requisition` to *their* file. massingplan collided on
  three names.

## For consumers: what this replaces, and what it does not

Answering modelmaker's question directly, having read the modules concerned.

**Addition — `sov_build.py` stays yours.** It builds a schedule of values *from
a model estimate*, regrouping priced lines by cost code. Massing Bill starts
from an SOV that already exists and never derives one from a model. Different
seam entirely; the two compose — `sov_build` produces the schedule, this core
bills against it.

**Replacement — the money path.** `payapp.py` parses amounts with
`float(str(v).replace(",", ""))`. That is the failure mode this core exists to
prevent: a 200-line schedule summed as floats drifts, and a pay application out
by a penny is rejected. Anything computing a billable amount should move to
`core.money`.

**Addition — the requisition itself.** There is no G702 header arithmetic,
retainage engine or change-order handling on your side to supersede. That is the
gap, and it is the substantial part of what this offers.

The waiver-versus-payment reconciliation in `payapp.py` overlaps with this
product's `WAIVER-*` tie-out rules, but those need the full application model, so
they are **not** in the core and are not part of what is being offered. Keep
yours.

Shape guarantee: `compute_application` returns a frozen `Application` dataclass
whose field names are the G702 line numbers. Those names are the published
surface and will not change within 1.x.
