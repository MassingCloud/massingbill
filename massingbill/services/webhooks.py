"""Outbound webhooks: signing, queueing, delivery and the delivery log.

**Nothing here sends during a request.** :func:`emit` writes ``PENDING`` rows in
the same transaction as the change that caused them and returns; :func:`drain`
does the sending, from a CLI command or a cron job. Two reasons, and the second
is the one that bites: an HTTP call inside a request makes the user wait for
someone else's server, and a webhook sent before the transaction commits
announces an event that may yet be rolled back.

The signature is byte-compatible with massing's existing scheme -- lowercase hex
HMAC-SHA256 over the exact request body, in ``X-Massing-Signature``, matching
``hash_hmac('sha256', $body, $secret)`` in ``class-cloud-sync.php``. A standalone
customer points this at their own ERP; massing.cloud is later just another
subscription row, verifying with the code already in their docs.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from flask import current_app

from massingbill.errors import ValidationError
from massingbill.extensions import db
from massingbill.models import (
    DeliveryStatus,
    Organization,
    User,
    WebhookDelivery,
    WebhookEvent,
    WebhookSubscription,
    utcnow,
)
from massingbill.services import audit
from massingbill.services.crypto import SecretBox


def _settings() -> Any:
    """The live ``Settings`` object.

    Read through here rather than from ``app.config["MASSINGBILL_..."]``: only a
    handful of settings are copied into the Flask config individually, so a
    ``config.get("MASSINGBILL_WEBHOOK_MAX_ATTEMPTS", 6)`` silently returns the
    default forever and the environment variable does nothing.
    """
    return current_app.config["MASSINGBILL_SETTINGS"]


def _box() -> SecretBox:
    return SecretBox(_settings().encryption_key)


SIGNATURE_HEADER = "X-Massing-Signature"
EVENT_HEADER = "X-Massing-Event"
DELIVERY_HEADER = "X-Massing-Delivery"

USER_AGENT = "MassingBill-Webhook/1"


def sign(body: bytes, secret: str) -> str:
    """Lowercase hex HMAC-SHA256 of the exact bytes sent.

    Deliberately identical to the reference verifier massing publishes, so a
    subscriber written against either side needs no second implementation.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: str) -> bool:
    """The other half, so tests and the SDK can check what we produced."""
    return hmac.compare_digest(sign(body, secret), signature)


def serialize(payload: dict[str, Any]) -> bytes:
    """The exact bytes that get signed, stored and sent.

    Sorted keys and no incidental whitespace: the signature covers bytes, so a
    payload that re-serializes differently later cannot be checked against the
    signature we recorded for it.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ── Subscriptions ───────────────────────────────────────────────────────────


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("A webhook URL must be http or https.")
    if not parsed.hostname:
        raise ValidationError("A webhook URL needs a host.")

    if _settings().webhook_allow_private_targets:
        return url

    # Resolve and check every address the name maps to. Checking only the first
    # leaves DNS free to answer with a public address once and a private one on
    # the request that matters.
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValidationError(f"Could not resolve {parsed.hostname}.") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local:
            raise ValidationError(
                f"{parsed.hostname} resolves to {address}, which this deployment "
                "does not allow as a webhook target."
            )
    return url


def subscribe(
    organization: Organization,
    *,
    url: str,
    secret: str,
    events: list[WebhookEvent] | None = None,
    description: str = "",
    actor: User | None = None,
) -> WebhookSubscription:
    if not secret:
        raise ValidationError("A webhook subscription needs a signing secret.")

    subscription = WebhookSubscription(
        organization_id=organization.id,
        url=_validate_url(url),
        description=description,
        events=" ".join(sorted(str(e) for e in events)) if events else "",
        secret_encrypted=_box().encrypt(secret),
    )
    db.session.add(subscription)
    db.session.flush()

    audit.record(
        organization.id,
        "webhook.subscribed",
        actor_id=actor.id if actor else None,
        entity_type="webhook_subscription",
        entity_id=subscription.id,
        after={"url": url, "events": sorted(str(e) for e in subscription.event_set)},
    )
    return subscription


def subscriptions_for(organization_id: str) -> list[WebhookSubscription]:
    return list(
        db.session.scalars(
            db.select(WebhookSubscription)
            .where(WebhookSubscription.organization_id == organization_id)
            .order_by(WebhookSubscription.created_at.desc())
        )
    )


# ── Emitting ────────────────────────────────────────────────────────────────


def emit(
    organization_id: str,
    event: WebhookEvent,
    payload: dict[str, Any],
) -> list[WebhookDelivery]:
    """Queue one event for every subscriber that wants it.

    Flushes rather than commits, so the deliveries land in the caller's
    transaction: if the change is rolled back, the announcement goes with it.
    """
    envelope = {
        "event": str(event),
        "organization_id": organization_id,
        # Whole seconds, matching the audit chain: a timestamp that is part of
        # signed bytes should not carry precision the database may not keep.
        "occurred_at": utcnow().replace(microsecond=0).isoformat(),
        "data": payload,
    }
    body = serialize(envelope)

    queued: list[WebhookDelivery] = []
    for subscription in subscriptions_for(organization_id):
        if not subscription.wants(event):
            continue
        delivery = WebhookDelivery(
            organization_id=organization_id,
            subscription_id=subscription.id,
            event=event,
            payload=body.decode("utf-8"),
            signature=sign(body, _box().decrypt(subscription.secret_encrypted)),
            status=DeliveryStatus.PENDING,
            next_attempt_at=utcnow(),
        )
        db.session.add(delivery)
        queued.append(delivery)

    if queued:
        db.session.flush()
    return queued


# ── Delivering ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Attempt:
    status_code: int | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


class Transport(Protocol):
    """How a delivery reaches the network.

    A seam, not an abstraction for its own sake: the test suite runs with
    outbound sockets refused, so the only way to test delivery, retry and
    backoff is to substitute this.
    """

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> Attempt: ...


class UrllibTransport:
    """Standard-library HTTP. No new dependency for one POST."""

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> Attempt:
        # The URL is a target the customer configured for their own ERP;
        # scheme and address policy are enforced in ``_validate_url``.
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return Attempt(status_code=response.status)
        except urllib.error.HTTPError as exc:
            return Attempt(status_code=exc.code, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - any failure is a failed attempt
            return Attempt(status_code=None, error=str(exc)[:500])


def backoff(attempts: int) -> timedelta:
    """Exponential, capped at an hour.

    1m, 2m, 4m, 8m, 16m, 32m, then hourly. Long enough that a subscriber's
    deploy finishes before we give up, short enough to be useful.
    """
    return min(timedelta(minutes=2 ** max(0, attempts - 1)), timedelta(hours=1))


def pending(limit: int = 100) -> list[WebhookDelivery]:
    now = utcnow()
    return list(
        db.session.scalars(
            db.select(WebhookDelivery)
            .where(
                WebhookDelivery.status.in_((DeliveryStatus.PENDING, DeliveryStatus.FAILED)),
                WebhookDelivery.next_attempt_at.is_not(None),
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
    )


def drain(*, transport: Transport | None = None, limit: int = 100) -> dict[str, int]:
    """Attempt every delivery that is due. Returns a small tally."""
    sender = transport or UrllibTransport()
    settings = _settings()
    max_attempts = settings.webhook_max_attempts
    disable_after = settings.webhook_disable_after_failures
    timeout = settings.webhook_timeout_seconds

    tally = {"delivered": 0, "failed": 0, "abandoned": 0}

    for delivery in pending(limit):
        subscription = delivery.subscription
        body = delivery.payload.encode("utf-8")
        attempt = sender.post(
            subscription.url,
            body,
            {
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                SIGNATURE_HEADER: delivery.signature,
                EVENT_HEADER: str(delivery.event),
                DELIVERY_HEADER: delivery.id,
            },
            timeout,
        )
        delivery.attempts += 1
        delivery.response_status = attempt.status_code

        if attempt.ok:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = utcnow()
            delivery.next_attempt_at = None
            delivery.error = ""
            subscription.consecutive_failures = 0
            tally["delivered"] += 1
            continue

        delivery.error = attempt.error or f"HTTP {attempt.status_code}"
        subscription.consecutive_failures += 1

        if delivery.attempts >= max_attempts:
            delivery.status = DeliveryStatus.ABANDONED
            delivery.next_attempt_at = None
            tally["abandoned"] += 1
        else:
            delivery.status = DeliveryStatus.FAILED
            delivery.next_attempt_at = utcnow() + backoff(delivery.attempts)
            tally["failed"] += 1

        if subscription.consecutive_failures >= disable_after and subscription.disabled_at is None:
            subscription.disabled_at = utcnow()
            audit.record(
                subscription.organization_id,
                "webhook.disabled",
                entity_type="webhook_subscription",
                entity_id=subscription.id,
                after={"url": subscription.url, "reason": "consecutive delivery failures"},
            )

    db.session.commit()
    return tally
