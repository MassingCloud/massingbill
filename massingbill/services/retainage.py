"""Retainage -- the ORM's way in to :mod:`massingbill.core.retainage`.

The engine moved into the core, where it computes over a
:class:`~massingbill.core.retainage.RetainageSpec` value and needs no database.
This module is the adapter: it turns the stored ``RetainageRule`` row into that
value, and re-exports the rest so existing callers are unaffected.

Six fields, one direction, no logic. If a rule ever needs interpreting rather
than copying, that interpretation belongs in the core with the arithmetic it
serves -- not here, where nothing tests it.
"""

from __future__ import annotations

from massingbill.core.enums import RetainageMode
from massingbill.core.retainage import (
    LineBasis,
    LineRetainage,
    RetainageResult,
    RetainageSpec,
    compute,
    effective_rates,
    exceeds_statutory_cap,
)
from massingbill.models import RetainageRule

__all__ = [
    "LineBasis",
    "LineRetainage",
    "RetainageResult",
    "RetainageSpec",
    "compute",
    "effective_rates",
    "exceeds_statutory_cap",
    "spec_for",
]


def spec_for(rule: RetainageRule) -> RetainageSpec:
    """The stored rule, as the plain value the engine takes."""
    return RetainageSpec(
        mode=RetainageMode(rule.mode),
        rate_work_bp=rule.rate_work_bp,
        rate_stored_bp=rule.rate_stored_bp,
        reduction_threshold_bp=rule.reduction_threshold_bp,
        reduced_rate_bp=rule.reduced_rate_bp,
        statutory_cap_bp=rule.statutory_cap_bp,
    )
