"""Fixtures for building a realistic tenant quickly.

Deliberately plain functions rather than a factory library: the tests are about
authorization and arithmetic, and a reader should be able to see exactly what
rows exist without learning another DSL.
"""

from __future__ import annotations

from dataclasses import dataclass

from massingbill.extensions import db
from massingbill.models import (
    Organization,
    PrimeContract,
    Project,
    RetainageMode,
    RetainageRule,
    Role,
    ScheduleOfValues,
    User,
)
from massingbill.services import accounts
from massingbill.services import sov as sov_service
from massingbill.services.money import cents

PASSWORD = "correct-horse-battery-staple"


@dataclass
class Tenant:
    """One organization with a user per role, a project, a contract and an SOV."""

    organization: Organization
    users: dict[Role, User]
    project: Project
    contract: PrimeContract
    schedule: ScheduleOfValues

    def user(self, role: Role) -> User:
        return self.users[role]


def make_user(email: str, *, name: str = "") -> User:
    return accounts.create_user(email, PASSWORD, name=name)


def make_tenant(slug: str, *, contract_sum_cents: int = 1_245_000_000) -> Tenant:
    """Build a complete tenant.

    The default contract sum is $12,450,000.00 -- the golden project from
    SPEC.md 7.2, so P3 can build on the same fixture.
    """
    owner = make_user(f"owner@{slug}.example", name="Owner")
    organization = accounts.create_organization(f"{slug.title()} Construction", owner)

    users = {Role.OWNER: owner}
    for role in Role:
        if role == Role.OWNER:
            continue
        member = make_user(f"{role}@{slug}.example", name=role.label)
        accounts.add_member(organization, member, role, actor=owner)
        users[role] = member

    project = Project(
        organization_id=organization.id,
        number="2026-001",
        # Distinct per tenant so a leak test cannot pass by coincidence.
        name=f"{slug.title()} Riverside Medical Office Building",
        jurisdiction_state="CA",
    )
    db.session.add(project)
    db.session.flush()

    rule = RetainageRule(
        organization_id=organization.id,
        mode=RetainageMode.SPLIT,
        rate_work_bp=1000,
        rate_stored_bp=1000,
    )
    db.session.add(rule)
    db.session.flush()

    contract = PrimeContract(
        organization_id=organization.id,
        project_id=project.id,
        number="PC-001",
        original_contract_sum_cents=contract_sum_cents,
        retainage_rule_id=rule.id,
    )
    db.session.add(contract)
    db.session.flush()

    schedule = sov_service.create_schedule(contract, actor=owner)
    db.session.commit()

    return Tenant(
        organization=organization,
        users=users,
        project=project,
        contract=contract,
        schedule=schedule,
    )


def add_balanced_lines(tenant: Tenant, count: int = 3) -> None:
    """Fill the schedule so it ties exactly to the contract sum."""
    from massingbill.services.money import allocate

    total = cents(tenant.contract.original_contract_sum_cents)
    shares = allocate(total, [cents(1)] * count)

    for index, share in enumerate(shares, start=1):
        sov_service.add_line(
            tenant.schedule,
            sov_service.LineInput(
                item_no=f"{index:03d}",
                description=f"Division {index:02d} work",
                scheduled_value_cents=share,
                csi_code=f"{index:02d}",
            ),
            actor=tenant.user(Role.OWNER),
        )
    db.session.commit()


def sign_in(client: object, user: User) -> None:
    """Sign in through the real form, so session state matches production."""
    response = client.post(  # type: ignore[attr-defined]
        "/auth/sign-in",
        data={"email": user.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), f"sign-in failed: {response.status_code}"
