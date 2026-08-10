# Massing Bill Python client

Standard library only. Copy the package, or `pip install -e sdk/python`.

```python
from massingbill_client import MassingBillClient, cents

client = MassingBillClient(api_key="mbil_...", base_url="https://billing.example.com")

for application in client.applications(status="submitted"):
    report = client.tieout(application["id"])
    if not report["ok"]:
        print(application["number"], "—", report["summary"])
        for finding in report["findings"]:
            print("   ", finding["rule_id"], finding["message"])
```

## Amounts are integer cents

Every money field is an object:

```json
{ "cents": 34700000, "amount": "347000.00", "currency": "USD" }
```

`cents` is authoritative. `amount` is the same number as a decimal string, for
systems that will not take integer minor units. Use `cents(...)` to read one:

```python
from massingbill_client import cents

due = cents(application["line8_current_payment_due"])
```

Do not do arithmetic on `amount`, and never put an amount into a float. The
entire engine exists to keep a pay application from being rejected over a penny.

## Verifying a webhook

Pass the **raw bytes** your framework received. The signature covers bytes, and
re-serializing the parsed JSON will not reproduce them.

```python
from flask import request
from massingbill_client import verify_webhook


@app.post("/hooks/massingbill")
def hook():
    if not verify_webhook(request.get_data(), request.headers["X-Massing-Signature"], SECRET):
        return "", 401
    event = request.get_json()
    ...
```

The scheme is identical to massing.cloud's, so an existing verifier works
unchanged: lowercase hex HMAC-SHA256 of the body, no `sha256=` prefix.

## Getting a key

```bash
massingbill apikey mint --organization <id> --name "ERP sync" --scope application:read --scope project:read
```

The token is printed once and never again — only its digest is stored.
