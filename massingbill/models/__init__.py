"""Model package.

Importing this module imports every mapped class, so Alembic's autogenerate
sees the full metadata and SQLAlchemy can resolve every string annotation.
"""

from __future__ import annotations

from .application import (
    EDITABLE_STATUSES,
    ISSUED_STATUSES,
    Application,
    ApplicationLine,
    ApplicationSnapshot,
    ApplicationStatus,
    Certification,
)
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
from .change import (
    ChangeOrder,
    ChangeOrderLine,
    ChangeOrderStatus,
    ChangeOrderType,
    PcoStatus,
    PotentialChangeOrder,
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
from .stored import StorageLocation, StoredMaterial
from .user import User

__all__ = [
    "EDITABLE_STATUSES",
    "GENESIS_HASH",
    "INTERNAL_ROLES",
    "ISSUED_STATUSES",
    "ROLE_LABELS",
    "Application",
    "ApplicationLine",
    "ApplicationSnapshot",
    "ApplicationStatus",
    "AuditEvent",
    "Base",
    "Certification",
    "ChangeOrder",
    "ChangeOrderLine",
    "ChangeOrderStatus",
    "ChangeOrderType",
    "ContractParty",
    "CostCode",
    "FormStyle",
    "Invitation",
    "Membership",
    "Organization",
    "PartyRole",
    "PcoStatus",
    "PeriodConvention",
    "PotentialChangeOrder",
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
    "StorageLocation",
    "StoredMaterial",
    "TimestampMixin",
    "User",
    "UuidPrimaryKeyMixin",
    "bp_column",
    "money_column",
    "new_uuid",
    "utcnow",
]
