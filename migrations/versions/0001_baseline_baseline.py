"""Baseline.

P0 ships no domain tables -- organizations, projects, schedules of values and
applications arrive in P2 and P3. This empty revision exists so the migration
chain has a root from the first commit: the ``migrations`` CI job can round-trip
``upgrade -> downgrade base -> upgrade`` against it, and every later revision has
a parent rather than a second head.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-08
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Nothing to create yet."""


def downgrade() -> None:
    """Nothing to drop."""
