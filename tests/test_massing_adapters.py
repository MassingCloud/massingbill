"""The massing.cloud adapters (P9).

Fixture-only. The suite refuses outbound sockets, and what is worth testing here
is our own behaviour anyway -- especially the failure behaviour, which is the
part a customer actually experiences and the part nobody exercises by accident.

The governing rule: **a network failure must not stop a contractor billing.**
A pay application is due on a date somebody else chose, and "our licence server
was down" is not a reason a GC can give an owner. Most of these tests exist to
hold that line.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

pytestmark = pytest.mark.adapter

entitlement_module = pytest.importorskip(
    "massingbill.services.entitlement.massing_cloud",
    reason="the massing.cloud adapter is not installed",
)
vault_module = pytest.importorskip(
    "massingbill.services.storage.massing_vault",
    reason="the massing vault adapter is not installed",
)
requests = pytest.importorskip("requests")

from massingbill.services.entitlement.base import UNLIMITED  # noqa: E402
from massingbill.services.storage.base import StoragePointer  # noqa: E402

MassingCloudProvider = entitlement_module.MassingCloudProvider
MassingVaultStorage = vault_module.MassingVaultStorage


class FakeResponse:
    def __init__(self, payload: Any = None, *, status: int = 200, content: bytes = b"") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> Any:
        return self._payload


ENTITLED = {
    "tier": "commercial",
    "entitled": True,
    "status": "active",
    "expires_at": "2027-01-01T00:00:00Z",
    "seats": {"limit": 25, "used": 3},
    "limits": {"gc_billing": True, "billing_projects": 50, "esign": True},
}


# ── Entitlements ────────────────────────────────────────────────────────────


def test_an_api_key_is_required() -> None:
    with pytest.raises(ValueError, match="API key"):
        MassingCloudProvider(api_key="")


def test_massings_entitlement_shape_maps_straight_across(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field names were matched from P0 precisely so this is a read rather
    than a translation."""
    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(entitlement_module.requests, "get", lambda *a, **k: FakeResponse(ENTITLED))

    e = provider.effective("org-1")

    assert e.tier == "commercial"
    assert e.entitled
    assert e.seats.limit == 25
    assert e.seats.used == 3
    assert e.limit("billing_projects") == 50
    assert e.allows("gc_billing")
    assert e.source == "massing_cloud"


def test_a_capability_massing_did_not_grant_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Entitlement.allows` defaults a missing key to True, which is right for a
    standalone install and wrong for a metered one. The adapter fills the gap."""
    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(entitlement_module.requests, "get", lambda *a, **k: FakeResponse(ENTITLED))

    e = provider.effective("org-1")

    assert not e.allows("custom_forms"), "an ungranted capability read as permitted"
    assert not e.allows("sub_tier_billing")


def test_the_answer_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def counted(*args: Any, **kwargs: Any) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(ENTITLED)

    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(entitlement_module.requests, "get", counted)

    provider.effective("org-1")
    provider.effective("org-1")

    assert calls["n"] == 1, "the entitlement was fetched on every page load"


def test_an_outage_serves_the_last_good_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point. A week of outage must not stop the monthly requisition."""
    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(entitlement_module.requests, "get", lambda *a, **k: FakeResponse(ENTITLED))
    provider.effective("org-1")

    def explode(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("massing.cloud is down")

    monkeypatch.setattr(entitlement_module.requests, "get", explode)
    monkeypatch.setattr(entitlement_module.time, "monotonic", lambda: 10_000.0)

    e = provider.effective("org-1")

    assert e.entitled, "an outage revoked a paying customer's entitlement"
    assert e.tier == "commercial"


def test_past_the_grace_window_it_degrades_to_read_only_not_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data is never destroyed and everything stays readable and exportable.
    Refusing outright would make our outage look like their lapsed plan."""
    provider = MassingCloudProvider(api_key="mcds_x")

    def explode(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(entitlement_module.requests, "get", explode)
    e = provider.effective("org-never-seen")

    assert e.read_only
    assert e.status == "unreachable"


def test_malformed_json_is_treated_as_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A storefront that returns an HTML error page must not crash a page load."""
    provider = MassingCloudProvider(api_key="mcds_x")

    class Broken(FakeResponse):
        def json(self) -> Any:
            raise ValueError("not json")

    monkeypatch.setattr(entitlement_module.requests, "get", lambda *a, **k: Broken())
    assert provider.effective("org-1").status == "unreachable"


def test_every_call_carries_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An un-timed-out call in a request path is an outage that looks like a
    hang, which is worse because nothing reports it."""
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> FakeResponse:
        seen.update(kwargs)
        return FakeResponse(ENTITLED)

    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(entitlement_module.requests, "get", capture)
    provider.effective("org-1")

    assert seen.get("timeout"), "no timeout on the entitlement call"


def test_the_bearer_header_matches_massings_own_convention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> FakeResponse:
        seen.update(kwargs)
        return FakeResponse(ENTITLED)

    provider = MassingCloudProvider(api_key="mcds_abc")
    monkeypatch.setattr(entitlement_module.requests, "get", capture)
    provider.effective("org-1")

    assert seen["headers"]["Authorization"] == "Bearer mcds_abc"


# ── Seats ───────────────────────────────────────────────────────────────────


def test_a_seat_is_granted_when_massing_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(
        entitlement_module.requests,
        "post",
        lambda *a, **k: FakeResponse({"granted": True, "seats": {"limit": 25, "used": 4}}),
    )

    result = provider.claim_seat("org-1", "user-1", "instance-1")

    assert result.granted
    assert result.seats.used == 4


def test_a_seat_is_refused_when_the_plan_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MassingCloudProvider(api_key="mcds_x")
    monkeypatch.setattr(
        entitlement_module.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"granted": False, "seats": {"limit": 5, "used": 5}, "reason": "no seats left"}
        ),
    )

    result = provider.claim_seat("org-1", "user-1", "instance-1")

    assert not result.granted
    assert "no seats" in result.reason


def test_an_unreachable_seat_service_grants_the_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seat accounting is a billing concern. Locking a PM out of a pay
    application on the 25th because we could not reach a counter makes our
    problem theirs."""
    provider = MassingCloudProvider(api_key="mcds_x")

    def explode(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(entitlement_module.requests, "post", explode)
    result = provider.claim_seat("org-1", "user-1", "instance-1")

    assert result.granted
    assert result.seats.limit == UNLIMITED
    assert "unreachable" in result.reason


def test_releasing_a_seat_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs at sign-out. An exception there would be a failed logout."""
    provider = MassingCloudProvider(api_key="mcds_x")

    def explode(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(entitlement_module.requests, "delete", explode)
    assert provider.release_seat("org-1", "instance-1") is None


# ── The vault ───────────────────────────────────────────────────────────────


def test_the_vault_needs_an_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        MassingVaultStorage(api_key="")


def test_storing_records_the_digest_of_what_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(vault_module.requests, "put", lambda *a, **k: FakeResponse({}))

    pointer = vault.put("app-1.pdf", io.BytesIO(b"%PDF-1.7"), content_type="application/pdf")

    assert pointer.sha256 == hashlib.sha256(b"%PDF-1.7").hexdigest()
    assert pointer.size == 8
    assert pointer.backend == "massing_vault"


def test_a_vault_that_reports_a_different_digest_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that re-encoded or altered the bytes has kept a different
    document from the one that was signed, and only the digest would say so."""
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(
        vault_module.requests, "put", lambda *a, **k: FakeResponse({"sha256": "deadbeef"})
    )

    with pytest.raises(ValueError, match="different digest"):
        vault.put("waiver.pdf", io.BytesIO(b"as signed"), content_type="application/pdf")


def test_reading_back_verifies_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    payload = b"as signed"
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(vault_module.requests, "get", lambda *a, **k: FakeResponse(content=payload))

    pointer = StoragePointer(
        backend="massing_vault",
        key="waiver.pdf",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert vault.open(pointer).read() == payload


def test_a_tampered_document_is_refused_rather_than_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(
        vault_module.requests, "get", lambda *a, **k: FakeResponse(content=b"tampered")
    )

    pointer = StoragePointer(backend="massing_vault", key="waiver.pdf", size=9, sha256="0" * 64)
    with pytest.raises(ValueError, match="no longer matches"):
        vault.open(pointer)


def test_a_missing_object_is_absent_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(vault_module.requests, "head", lambda *a, **k: FakeResponse(status=404))

    assert not vault.exists(StoragePointer(backend="massing_vault", key="x", size=0, sha256=""))


def test_deleting_something_already_gone_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletes are retried and run from cleanup paths; 404 is the desired state."""
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(vault_module.requests, "delete", lambda *a, **k: FakeResponse(status=404))

    assert vault.delete(StoragePointer(backend="massing_vault", key="x", size=0, sha256="")) is None


def test_the_vault_never_hands_back_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed vault link is a bearer token for a financial document. Pointers
    stay opaque and downloads stay mediated by our own ownership check."""
    vault = MassingVaultStorage(api_key="mcds_x")
    monkeypatch.setattr(vault_module.requests, "put", lambda *a, **k: FakeResponse({}))

    pointer = vault.put("x.pdf", io.BytesIO(b"x"), content_type="application/pdf")

    for value in (pointer.key, pointer.backend, pointer.content_type):
        assert "http" not in value.lower()
