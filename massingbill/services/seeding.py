"""Seeding reference data into an organization.

Idempotent: running it twice adds nothing. Seeded rows are marked ``is_seeded``
so a later edition of MasterFormat can be reconciled without touching a
contractor's own cost codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from massingbill.extensions import db
from massingbill.models import CostCode, Organization

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


def _load(name: str) -> dict[str, Any]:
    with (SEED_DIR / name).open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle)
    return data


def seed_cost_codes(organization: Organization) -> int:
    """Load the CSI MasterFormat divisions. Returns how many were added."""
    divisions = _load("masterformat.yaml")["divisions"]

    existing = set(
        db.session.scalars(select(CostCode.code).where(CostCode.organization_id == organization.id))
    )

    added = 0
    for division in divisions:
        code = str(division["code"])
        if code in existing:
            continue
        db.session.add(
            CostCode(
                organization_id=organization.id,
                code=code,
                title=str(division["title"]),
                division=code,
                is_seeded=True,
            )
        )
        added += 1

    db.session.flush()
    return added
