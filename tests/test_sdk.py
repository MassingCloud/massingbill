"""The shipped Python client, checked against the server it talks to.

An SDK tested only against its own assumptions is a second source of truth. The
tests that matter here are the ones where client and server have to agree:
webhook verification, and the shape of a money field.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask
from massingbill_client import MassingBillClient, MassingBillError, cents, verify_webhook

from massingbill.extensions import db
from massingbill.models import WebhookEvent
from massingbill.services import webhooks
from tests.factories import Tenant, add_balanced_lines, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    built = make_tenant("acme")
    add_balanced_lines(built)
    return built


# ── The cross-system contract ───────────────────────────────────────────────


def test_the_sdk_verifies_a_webhook_this_server_actually_produced(tenant: Tenant) -> None:
    """Client and server, end to end, over the real signing path."""
    webhooks.subscribe(tenant.organization, url="https://erp.example/h", secret="shared")
    db.session.commit()

    delivery = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {"n": 1})[
        0
    ]
    db.session.commit()

    assert verify_webhook(delivery.payload.encode("utf-8"), delivery.signature, "shared")


def test_the_sdk_rejects_a_tampered_body(tenant: Tenant) -> None:
    webhooks.subscribe(tenant.organization, url="https://erp.example/h", secret="shared")
    db.session.commit()
    delivery = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})[0]
    db.session.commit()

    tampered = json.loads(delivery.payload)
    tampered["data"] = {"n": 999}

    assert not verify_webhook(json.dumps(tampered).encode("utf-8"), delivery.signature, "shared")


def test_verification_needs_the_raw_bytes_not_a_reserialization(tenant: Tenant) -> None:
    """The documented footgun, pinned so the docs stay true.

    ``json.dumps`` of a parsed payload does not reproduce the bytes that were
    signed, and a subscriber who does that sees every delivery fail.
    """
    webhooks.subscribe(tenant.organization, url="https://erp.example/h", secret="shared")
    db.session.commit()
    delivery = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})[0]
    db.session.commit()

    reserialized = json.dumps(json.loads(delivery.payload)).encode("utf-8")
    assert reserialized != delivery.payload.encode("utf-8")
    assert not verify_webhook(reserialized, delivery.signature, "shared")


def test_cents_reads_the_authoritative_field() -> None:
    from massingbill.blueprints.api import money

    field = money(347_000_00)
    assert field is not None
    assert cents(field) == 347_000_00
    # And the decimal string is exactly that number, not a rounded float.
    assert field["amount"] == "347000.00"


def test_cents_of_nothing_is_zero() -> None:
    assert cents(None) == 0


# ── Client behaviour ────────────────────────────────────────────────────────


def test_a_client_needs_a_key() -> None:
    with pytest.raises(ValueError, match="API key is required"):
        MassingBillClient(api_key="")


def test_a_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    client = MassingBillClient(api_key="mbil_x_y", base_url="https://billing.example.com/")
    assert client.base_url == "https://billing.example.com"


def test_an_unreachable_server_raises_a_typed_error() -> None:
    """Loopback on a closed port: refused instantly, and never leaves the host.

    The suite blocks outbound sockets, so this doubles as proof the client is
    not quietly reaching anywhere else.
    """
    client = MassingBillClient(api_key="mbil_x_y", base_url="http://127.0.0.1:1", timeout=1.0)

    with pytest.raises(MassingBillError, match="Could not reach"):
        client.status()
