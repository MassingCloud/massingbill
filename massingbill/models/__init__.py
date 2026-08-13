"""Model package.

Importing this module imports every mapped class, so Alembic's autogenerate
sees the full metadata and SQLAlchemy can resolve every string annotation.
"""

from __future__ import annotations

from .apikey import (
    ALL_WEBHOOK_EVENTS,
    ApiKey,
    DeliveryStatus,
    WebhookDelivery,
    WebhookEvent,
    WebhookSubscription,
)
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
from .compliance import (
    COMPLIANCE_LABELS,
    ComplianceDoc,
    ComplianceKind,
    ComplianceRequirement,
)
from .deadline import (
    DEADLINE_LABELS,
    ClaimantRole,
    DayBasis,
    DeadlineAnchor,
    DeadlineKind,
    DeadlineRule,
)
from .handoff import SpentHandoff
from .organization import (
    INTERNAL_ROLES,
    ROLE_LABELS,
    Invitation,
    Membership,
    Organization,
    Role,
)
from .payment import Payment, PaymentMethod
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
from .subcontract import (
    SubApplication,
    SubApplicationStatus,
    Subcontract,
    SubcontractLine,
    SubcontractStatus,
)
from .user import User
from .waiver import (
    WAIVER_TYPE_LABELS,
    Signature,
    WaiverInstance,
    WaiverStatus,
    WaiverTemplate,
    WaiverType,
)

__all__ = [
    "ALL_WEBHOOK_EVENTS",
    "COMPLIANCE_LABELS",
    "DEADLINE_LABELS",
    "EDITABLE_STATUSES",
    "GENESIS_HASH",
    "INTERNAL_ROLES",
    "ISSUED_STATUSES",
    "ROLE_LABELS",
    "WAIVER_TYPE_LABELS",
    "ApiKey",
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
    "ClaimantRole",
    "ComplianceDoc",
    "ComplianceKind",
    "ComplianceRequirement",
    "ContractParty",
    "CostCode",
    "DayBasis",
    "DeadlineAnchor",
    "DeadlineKind",
    "DeadlineRule",
    "DeliveryStatus",
    "FormStyle",
    "Invitation",
    "Membership",
    "Organization",
    "PartyRole",
    "Payment",
    "PaymentMethod",
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
    "Signature",
    "SoftDeleteMixin",
    "SovLine",
    "SovStatus",
    "SpentHandoff",
    "StorageLocation",
    "StoredMaterial",
    "SubApplication",
    "SubApplicationStatus",
    "Subcontract",
    "SubcontractLine",
    "SubcontractStatus",
    "TimestampMixin",
    "User",
    "UuidPrimaryKeyMixin",
    "WaiverInstance",
    "WaiverStatus",
    "WaiverTemplate",
    "WaiverType",
    "WebhookDelivery",
    "WebhookEvent",
    "WebhookSubscription",
    "bp_column",
    "money_column",
    "new_uuid",
    "utcnow",
]
