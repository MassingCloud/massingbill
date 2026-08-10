"""Outbound webhooks: signing, queueing, retry and the delivery log.

The signature test is the important one. It is checked against the verifier
massing publishes in ``docs/05-rest-api.md``, copied verbatim below rather than
imported, so that if either side ever changes its scheme this fails instead of
both drifting together.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from flask import Flask

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import DeliveryStatus, WebhookEvent
from massingbill.services import webhooks
from tests.factories import Tenant, make_tenant


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return make_tenant("acme")


@pytest.fixture
def subscription(tenant: Tenant):
    sub = webhooks.subscribe(
        tenant.organization,
        url="https://erp.example.com/hooks/massingbill",
        secret="shared-secret",
        events=[WebhookEvent.APPLICATION_SUBMITTED],
    )
    db.session.commit()
    return sub


class FakeTransport:
    """Records what it was asked to send and answers with a scripted status."""

    def __init__(self, *statuses: int | None) -> None:
        self.statuses = list(statuses) or [200]
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float):
        self.sent.append((url, body, headers))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            return webhooks.Attempt(status_code=None, error="connection refused")
        return webhooks.Attempt(status_code=status)


# ── Signing ─────────────────────────────────────────────────────────────────


def massing_reference_verify(body: bytes, signature: str, secret: str) -> bool:
    """The verifier from massing's own docs, reproduced exactly.

    Copied rather than imported on purpose: this is a cross-system contract,
    and a shared helper would let both sides drift in step and still pass.
    """
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def test_our_signature_satisfies_massings_reference_verifier() -> None:
    body = b'{"event":"application.submitted"}'
    assert massing_reference_verify(body, webhooks.sign(body, "s3cret"), "s3cret")


def test_the_signature_is_lowercase_hex_with_no_prefix() -> None:
    """``sha256=`` prefixes are a GitHub convention, not massing's.

    ``hash_hmac('sha256', $body, $secret)`` in PHP returns bare lowercase hex,
    and a subscriber comparing strings will reject anything else.
    """
    signature = webhooks.sign(b"x", "k")
    assert signature == signature.lower()
    assert len(signature) == 64
    assert not signature.startswith("sha256")
    int(signature, 16)  # raises if it is not hex


def test_a_tampered_body_fails_verification() -> None:
    signature = webhooks.sign(b'{"amount":100}', "k")
    assert not webhooks.verify(b'{"amount":900}', signature, "k")


def test_the_signed_bytes_are_stable(subscription) -> None:
    """The signature covers bytes, so serialization must not wobble."""
    first = webhooks.serialize({"b": 2, "a": 1})
    second = webhooks.serialize({"a": 1, "b": 2})
    assert first == second == b'{"a":1,"b":2}'


# ── Emitting ────────────────────────────────────────────────────────────────


def test_emitting_queues_a_delivery_without_sending(tenant: Tenant, subscription) -> None:
    """Nothing reaches the network during a request.

    The suite blocks outbound sockets, so if ``emit`` ever sends inline this
    test fails with a NetworkBlockedError rather than passing quietly.
    """
    queued = webhooks.emit(
        tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {"application_id": "abc"}
    )
    db.session.commit()

    assert len(queued) == 1
    assert queued[0].status == DeliveryStatus.PENDING
    assert queued[0].delivered_at is None


def test_the_payload_is_signed_with_the_subscribers_own_secret(
    tenant: Tenant, subscription
) -> None:
    queued = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {"n": 1})
    db.session.commit()

    delivery = queued[0]
    assert massing_reference_verify(
        delivery.payload.encode("utf-8"), delivery.signature, "shared-secret"
    )


def test_a_subscriber_only_gets_the_events_it_asked_for(tenant: Tenant, subscription) -> None:
    queued = webhooks.emit(tenant.organization.id, WebhookEvent.WAIVER_SIGNED, {})
    db.session.commit()
    assert queued == []


def test_an_empty_event_list_means_every_event(tenant: Tenant) -> None:
    webhooks.subscribe(tenant.organization, url="https://x.example/h", secret="s")
    db.session.commit()

    for event in WebhookEvent:
        assert webhooks.emit(tenant.organization.id, event, {}), event


def test_the_envelope_carries_the_event_and_the_organization(tenant: Tenant, subscription) -> None:
    queued = webhooks.emit(
        tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {"application_id": "abc"}
    )
    db.session.commit()

    envelope = json.loads(queued[0].payload)
    assert envelope["event"] == "application.submitted"
    assert envelope["organization_id"] == tenant.organization.id
    assert envelope["data"] == {"application_id": "abc"}


def test_events_do_not_cross_tenants(tenant: Tenant, subscription, app: Flask) -> None:
    stranger = make_tenant("rival")
    db.session.commit()

    assert webhooks.emit(stranger.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {}) == []


# ── Delivering ──────────────────────────────────────────────────────────────


def test_a_successful_delivery_is_marked_delivered(tenant: Tenant, subscription) -> None:
    webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()

    transport = FakeTransport(200)
    tally = webhooks.drain(transport=transport)

    assert tally == {"delivered": 1, "failed": 0, "abandoned": 0}
    assert transport.sent[0][0] == "https://erp.example.com/hooks/massingbill"


def test_the_delivery_carries_the_massing_headers(tenant: Tenant, subscription) -> None:
    webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()

    transport = FakeTransport(200)
    webhooks.drain(transport=transport)

    _, body, headers = transport.sent[0]
    assert headers["X-Massing-Event"] == "application.submitted"
    assert massing_reference_verify(body, headers["X-Massing-Signature"], "shared-secret")


def test_a_failure_is_retried_with_backoff(tenant: Tenant, subscription) -> None:
    queued = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()
    delivery_id = queued[0].id

    webhooks.drain(transport=FakeTransport(500))

    delivery = db.session.get(type(queued[0]), delivery_id)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None


def test_retries_are_abandoned_rather_than_run_forever(
    tenant: Tenant, subscription, app: Flask
) -> None:
    app.config["MASSINGBILL_SETTINGS"].webhook_max_attempts = 2
    queued = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()
    delivery_id = queued[0].id
    model = type(queued[0])

    for _ in range(2):
        # Make the retry due immediately rather than waiting out the backoff.
        db.session.get(model, delivery_id).next_attempt_at = webhooks.utcnow()
        db.session.commit()
        webhooks.drain(transport=FakeTransport(500))

    assert db.session.get(model, delivery_id).status == DeliveryStatus.ABANDONED


def test_an_abandoned_delivery_is_kept_not_deleted(
    tenant: Tenant, subscription, app: Flask
) -> None:
    """ "We tried and gave up" is a different fact from "we never had it"."""
    app.config["MASSINGBILL_SETTINGS"].webhook_max_attempts = 1
    queued = webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()
    model, delivery_id = type(queued[0]), queued[0].id

    webhooks.drain(transport=FakeTransport(500))

    kept = db.session.get(model, delivery_id)
    assert kept is not None
    assert kept.status == DeliveryStatus.ABANDONED
    assert kept.error


def test_a_connection_failure_is_recorded_not_raised(tenant: Tenant, subscription) -> None:
    webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
    db.session.commit()

    tally = webhooks.drain(transport=FakeTransport(None))
    assert tally["failed"] == 1


def test_a_persistently_dead_endpoint_is_disabled(tenant: Tenant, subscription, app: Flask) -> None:
    app.config["MASSINGBILL_SETTINGS"].webhook_disable_after_failures = 2
    app.config["MASSINGBILL_SETTINGS"].webhook_max_attempts = 99

    for _ in range(2):
        webhooks.emit(tenant.organization.id, WebhookEvent.APPLICATION_SUBMITTED, {})
        db.session.commit()
        webhooks.drain(transport=FakeTransport(500))

    db.session.refresh(subscription)
    assert subscription.disabled_at is not None
    assert not subscription.wants(WebhookEvent.APPLICATION_SUBMITTED)


def test_backoff_is_exponential_and_capped() -> None:
    minutes = [webhooks.backoff(n).total_seconds() / 60 for n in range(1, 9)]
    assert minutes[:5] == [1, 2, 4, 8, 16]
    assert max(minutes) == 60


# ── Target validation ───────────────────────────────────────────────────────


def test_a_non_http_url_is_refused(tenant: Tenant) -> None:
    with pytest.raises(ValidationError, match="http or https"):
        webhooks.subscribe(tenant.organization, url="ftp://x.example/h", secret="s")


def test_private_targets_are_allowed_by_default(tenant: Tenant) -> None:
    """Self-hosting is the product: an ERP on 10.0.0.0/8 is the normal case."""
    sub = webhooks.subscribe(tenant.organization, url="http://10.0.0.5/hook", secret="s")
    assert sub.url == "http://10.0.0.5/hook"


def test_private_targets_can_be_refused_for_hosted_deployments(tenant: Tenant, app: Flask) -> None:
    """Where one tenant could otherwise aim a webhook at cloud metadata."""
    app.config["MASSINGBILL_SETTINGS"].webhook_allow_private_targets = False

    with pytest.raises(ValidationError, match="does not allow"):
        webhooks.subscribe(tenant.organization, url="http://169.254.169.254/latest", secret="s")


def test_the_signing_secret_is_encrypted_at_rest(tenant: Tenant, subscription) -> None:
    assert "shared-secret" not in subscription.secret_encrypted


def test_the_settings_are_actually_wired_to_the_environment(monkeypatch) -> None:
    """The bug this exists to prevent: reading a setting from a Flask config key
    that nothing ever writes.

    ``config.get("MASSINGBILL_WEBHOOK_MAX_ATTEMPTS", 6)`` returns 6 forever --
    only a few settings are copied into ``app.config`` individually, and the
    rest live on the ``Settings`` object. Tests that override the config key
    directly still pass, which is how it survives review.
    """
    from massingbill.config import Settings

    monkeypatch.setenv("MASSINGBILL_WEBHOOK_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MASSINGBILL_WEBHOOK_ALLOW_PRIVATE_TARGETS", "false")

    settings = Settings(secret_key="x" * 40, encryption_key="y" * 40)

    assert settings.webhook_max_attempts == 3
    assert settings.webhook_allow_private_targets is False


# ── The sweep ───────────────────────────────────────────────────────────────


def test_the_sweep_announces_an_open_period_that_will_not_submit(
    tenant: Tenant, subscription, app: Flask
) -> None:
    """``submit`` deliberately does not emit this event -- it raises, and the
    caller's rollback would take the event with it. The sweep is where the
    announcement actually survives, so this is what proves the event is
    reachable at all."""
    from massingbill.models import WebhookDelivery
    from massingbill.services import events

    webhooks.subscribe(
        tenant.organization,
        url="https://erp.example/tieout",
        secret="s",
        events=[WebhookEvent.TIEOUT_FAILED],
    )
    db.session.commit()

    failures = events.sweep_open_periods(tenant.organization.id)

    queued = list(
        db.session.scalars(
            db.select(WebhookDelivery).where(WebhookDelivery.event == WebhookEvent.TIEOUT_FAILED)
        )
    )
    assert len(queued) == len(failures)


def test_the_sweep_is_quiet_when_everything_balances(tenant: Tenant, app: Flask) -> None:
    from massingbill.services import events

    assert events.sweep_open_periods(tenant.organization.id) == []
