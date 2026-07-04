"""Policy validation errors (F29).

The flat F04 loader surfaces validation failures as pydantic ``ValidationError``
/ ``ValueError`` (there is no dedicated ``PolicyValidationError`` class in this
foundation — see the slice notes). F29 adds :class:`PolicyRuleError` for
conditional-rule-specific failures; it subclasses :class:`ValueError` so it is
raised cleanly from the ``Policy`` model validator and from the linter.
"""

from __future__ import annotations

from forge_contracts import ForgeError


class PolicyRuleError(ForgeError, ValueError):
    """A conditional rule (or rule block) failed validation."""


__all__ = ["PolicyRuleError"]
