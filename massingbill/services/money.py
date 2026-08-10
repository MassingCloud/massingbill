"""The money kernel -- now :mod:`massingbill.core.money`.

Kept as a re-export because sixty-odd modules import ``services.money`` and none
of them should have to care that the arithmetic moved somewhere with no
dependencies. New code should import from ``massingbill.core``; this module is
not deprecated so much as it is the old address.

A star import rather than a second list of names, deliberately: the surface is
declared once, in ``core/money.py``'s ``__all__``, so a function added there
cannot be silently missing here. Maintaining the list twice is how
``BP_SCALE`` went missing from this file for exactly one commit.

The money-discipline CI gate follows the implementation, not this file.
"""

from __future__ import annotations

from massingbill.core.money import *  # noqa: F403
from massingbill.core.money import __all__ as __all__
