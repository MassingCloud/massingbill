"""Model package.

Importing this module imports every mapped class, so Alembic's autogenerate
sees the full metadata and SQLAlchemy can resolve every string annotation.
"""

from __future__ import annotations

from .audit import GENESIS_HASH, AuditEvent
from .base import (
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    bp_column,
    money_column,
    new_uuid,
    utcnow,
)
from .organization import (
    INTERNAL_ROLES,
    ROLE_LABELS,
    Invitation,
    Membership,
    Organization,
    Role,
)
from .project import (
    ContractParty,
    FormStyle,
    PartyRole,
    PeriodConvention,
    PrimeContract,
    Project,
    ProjectStatus,
    RetainageMode,
    RetainageRule,
)
from .sov import CostCode, ScheduleOfValues, SovLine, SovStatus
from .user import User

__all__ = [
    "GENESIS_HASH",
    "INTERNAL_ROLES",
    "ROLE_LABELS",
    "AuditEvent",
    "Base",
    "ContractParty",
    "CostCode",
    "FormStyle",
    "Invitation",
    "Membership",
    "Organization",
    "PartyRole",
    "PeriodConvention",
    "PrimeContract",
    "Project",
    "ProjectStatus",
    "RetainageMode",
    "RetainageRule",
    "Role",
    "ScheduleOfValues",
    "SoftDeleteMixin",
    "SovLine",
    "SovStatus",
    "TimestampMixin",
    "User",
    "UuidPrimaryKeyMixin",
    "bp_column",
    "money_column",
    "new_uuid",
    "utcnow",
]
