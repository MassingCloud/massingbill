"""Entitlement enforcement.

Two halves, and the first matters more than the second.

**A standalone install must be unaffected.** SPEC.md 3.3 promises no licence, no
phone-home and no kill switch. Every gate added here is a no-op unless an
operator opted into a provider that says otherwise, and that is asserted first
so a regression shows up as "standalone stopped working" rather than as a
subtle cap somebody discovers at month-end.

**A capped provider must actually refuse**, at the specific operation, with a
message that says which one. Driven through the real service functions rather
than through ``limits`` directly, because the thing most likely to break is the
wiring -- a call site that was never added, or one added to the wrong function.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from flask import Flask

from massingbill.errors import EntitlementError
from massingbill.extensions import db
from massingbill.models import Project, Role, WaiverStatus, WaiverType
from massingbill.services import application as application_service
from massingbill.services import limits, subcontracts, waivers
from massingbill.services import sov as sov_service
from massingbill.services.entitlement import StandaloneProvider
from massingbill.services.entitlement.base import Entitlement, EntitlementProvider, SeatResult
from massingbill.services.money import cents
from tests.factories import Tenant, add_balanced_lines, make_tenant, sign_in


class FixedProvider(EntitlementProvider):
    """Answers with one entitlement, and counts how often it was asked."""

    name = "fixed"

    def __init__(self, **limit_values: Any) -> None:
        self.entitlement = Entitlement(tier="commercial", limits=dict(limit_values))
        self.calls = 0

    def effective(self, organization_id: str) -> Entitlement:
        self.calls += 1
        return self.entitlement

    def claim_seat(self, organization_id: str, user_id: str, instance: str) -> SeatResult:
        raise NotImplementedError


def cap(app: Flask, **limit_values: Any) -> FixedProvider:
    """Install a provider with these limits for the rest of the test."""
    provider = FixedProvider(**limit_values)
    app.extensions["massingbill_entitlement"] = provider
    return provider


# ── Standalone changes nothing ──────────────────────────────────────────────


def test_the_default_provider_is_standalone(app: Flask) -> None:
    assert isinstance(app.extensions["massingbill_entitlement"], StandaloneProvider)


@pytest.mark.parametrize(
    "capability",
    [limits.GC_BILLING, limits.SUB_TIER_BILLING, limits.ESIGN, limits.CUSTOM_FORMS],
)
def test_standalone_allows_every_capability(app: Flask, capability: str) -> None:
    limits.require(capability, "any-org", what="the thing")


@pytest.mark.parametrize("name", [limits.BILLING_PROJECTS, limits.BILLING_APPS_PER_MONTH])
def test_standalone_has_no_counted_cap(app: Flask, name: str) -> None:
    """A million of anything is still fine. An absent limit is unlimited, not
    zero -- which is the direction this has to fail in."""
    limits.require_within(name, 1_000_000, "any-org", what="things")


def test_an_unknown_flag_defaults_to_allowed(app: Flask) -> None:
    """A capability nobody has heard of must not lock a working install out.
    A typo in a flag name has to read as "granted", not as "denied"."""
    cap(app, gc_billing=True)
    limits.require("some_flag_added_later", "any-org", what="a future feature")


# ── A capped provider refuses, at the right place ───────────────────────────


def test_a_denied_capability_refuses(app: Flask) -> None:
    cap(app, gc_billing=False)

    with pytest.raises(EntitlementError, match="does not include GC billing"):
        limits.require(limits.GC_BILLING, "org", what="GC billing")


def test_a_reached_cap_refuses_and_says_the_numbers(app: Flask) -> None:
    cap(app, billing_projects=3)

    with pytest.raises(EntitlementError, match="allows 3 projects, and you have 3"):
        limits.require_within(limits.BILLING_PROJECTS, 3, "org", what="projects")


def test_under_the_cap_is_allowed(app: Flask) -> None:
    """The count is what exists *before* the new one, so equal-to-cap is the
    refusal and one-below is the last permitted create."""
    cap(app, billing_projects=3)
    limits.require_within(limits.BILLING_PROJECTS, 2, "org", what="projects")


def test_a_lapsed_subscription_denies_everything(app: Flask) -> None:
    provider = cap(app)
    provider.entitlement = Entitlement(tier="commercial", entitled=False, limits={})

    with pytest.raises(EntitlementError, match="read-only"):
        limits.require(limits.GC_BILLING, "org", what="GC billing")


def test_a_lapsed_subscription_refuses_counted_limits_too(app: Flask) -> None:
    """Not merely because a capability gate ran first. `within()` answers a
    question about counts and knows nothing about `entitled`, so a counted gate
    added on its own must still refuse -- even with the count comfortably under
    an unlimited cap."""
    provider = cap(app)
    provider.entitlement = Entitlement(tier="commercial", entitled=False, limits={})

    with pytest.raises(EntitlementError, match="read-only"):
        limits.require_within(limits.BILLING_PROJECTS, 0, "org", what="projects")


def test_the_refusal_carries_the_details_an_api_client_needs(app: Flask) -> None:
    cap(app, billing_projects=2)

    with pytest.raises(EntitlementError) as caught:
        limits.require_within(limits.BILLING_PROJECTS, 5, "org", what="projects")

    assert caught.value.status_code == 409
    assert caught.value.details == {
        "limit": limits.BILLING_PROJECTS,
        "cap": 2,
        "current": 5,
        "tier": "commercial",
    }


# ── Asked once, not once per question ───────────────────────────────────────


def test_the_provider_is_consulted_once_per_organization(app: Flask) -> None:
    """Two gates on one page must not be two round trips to massing.cloud."""
    provider = cap(app, gc_billing=True)

    for _ in range(5):
        limits.require(limits.GC_BILLING, "org-a", what="GC billing")
    limits.require(limits.GC_BILLING, "org-b", what="GC billing")

    assert provider.calls == 2, "one per organization, not one per question"


# ── Through the real call sites ─────────────────────────────────────────────


def billable(slug: str) -> Tenant:
    """A tenant that can actually open a period: lines entered and approved."""
    built = make_tenant(slug)
    add_balanced_lines(built)
    sov_service.approve(built.schedule, actor=built.user(Role.OWNER))
    db.session.commit()
    return built


@pytest.fixture
def tenant(app: Flask) -> Tenant:
    return billable("acme")


def test_opening_a_period_is_refused_without_gc_billing(app: Flask, tenant: Tenant) -> None:
    cap(app, gc_billing=False)

    with pytest.raises(EntitlementError, match="GC billing"):
        application_service.open_period(
            tenant.contract, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
        )


def test_opening_a_period_is_refused_at_the_monthly_cap(app: Flask, tenant: Tenant) -> None:
    """Nothing has been opened yet, so a cap of zero is the cleanest way to
    assert the count is consulted at all."""
    cap(app, gc_billing=True, billing_apps_per_month=0)

    with pytest.raises(EntitlementError, match="pay applications a month"):
        application_service.open_period(
            tenant.contract, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
        )


def test_the_monthly_count_is_per_organization(app: Flask, tenant: Tenant) -> None:
    """A busy neighbour must not exhaust this customer's allowance."""
    other = billable("rival")
    application_service.open_period(
        other.contract, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    )
    db.session.commit()

    cap(app, gc_billing=True, billing_apps_per_month=1)
    opened = application_service.open_period(
        tenant.contract, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    )

    assert opened.number == 1


def test_a_subcontract_is_refused_without_sub_tier_billing(app: Flask, tenant: Tenant) -> None:
    cap(app, sub_tier_billing=False)

    with pytest.raises(EntitlementError, match="subcontractor billing"):
        subcontracts.create(
            tenant.project, number="02-100", vendor_name="Ace Concrete", amount=500_00
        )


def test_signing_a_waiver_is_refused_without_esign(app: Flask, tenant: Tenant) -> None:
    # Capped *before* the setup below, because the entitlement is cached for the
    # life of the app context and the setup itself passes through a gate. In
    # production that context is one request, so the cache is a request-scoped
    # memo rather than something that can go stale under a user.
    cap(app, esign=False)

    waivers.seed_templates(tenant.organization)
    # New York prescribes no waiver wording, so the general form applies and the
    # waiver can actually be issued. In a statutory state this test would fail
    # one step earlier, on the empty-form refusal, and never reach the gate.
    tenant.project.jurisdiction_state = "NY"
    opened = application_service.open_period(
        tenant.contract, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    )
    waiver = waivers.request(
        opened,
        waiver_type=WaiverType.CONDITIONAL_PROGRESS,
        claimant="Ace Concrete",
        customer="Riverside Owner LLC",
        amount=cents(100_00),
    )
    db.session.flush()

    with pytest.raises(EntitlementError, match="electronic signature"):
        waivers.sign(waiver, signer_name="Pat Ace", consented=True)

    assert waiver.status is not WaiverStatus.SIGNED


def test_creating_a_project_is_refused_at_the_cap(client: Any, app: Flask, tenant: Tenant) -> None:
    """Through the route, because this is the one gate that lives in a view --
    the project count has no service function to hang it off."""
    before = db.session.query(Project).count()
    cap(app, gc_billing=True, billing_projects=before)

    sign_in(client, tenant.user(Role.OWNER))
    response = client.post(
        "/projects/new",
        data={"number": "P-999", "name": "Overflow", "jurisdiction_state": "CA"},
        follow_redirects=True,
    )

    assert response.status_code == 409
    assert b"allows 1 projects" in response.data, "409 for the entitlement, not for something else"
    assert db.session.query(Project).count() == before
