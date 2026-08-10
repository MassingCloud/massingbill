"""Vocabulary the calculation itself needs.

These live in the core rather than in ``models/`` because the arithmetic
genuinely depends on them -- a retainage computation cannot be written without
knowing what ``STEPPED`` means -- and the core is not allowed to import the ORM.

``models/`` imports *these*, so there is exactly one definition of each and a
persisted value cannot drift from the value the engine branches on.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RetainageMode"]


class RetainageMode(StrEnum):
    """How retainage is withheld.

    ``SPLIT`` is the G702 default: separate rates for completed work (line 5a)
    and stored material (line 5b). ``VARIABLE_LINE`` drives G703 column I.
    ``STEPPED`` reduces the rate once the project passes a completion threshold.
    """

    FLAT = "flat"
    SPLIT = "split"
    VARIABLE_LINE = "variable_line"
    STEPPED = "stepped"
