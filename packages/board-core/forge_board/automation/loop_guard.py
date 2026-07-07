"""Loop / cascade protection for the automation engine (F21).

Two structural guards bound runaway cascades (rule A's action triggers rule B's
trigger triggers A ...):

* a propagated **depth** counter: once ``envelope.depth >= MAX_DEPTH`` the next
  evaluation is aborted (``skipped_loop``), and
* **causation-chain cycle detection**: a rule already present in
  ``envelope.causation_chain`` is aborted before it fires a second time.

Action-emitted events carry ``depth + 1`` and ``causation_chain + [rule_id]``.
"""

from __future__ import annotations

import os
import uuid

from forge_contracts.automation import AutomationTriggerEnvelope

DEFAULT_MAX_DEPTH = 5


def resolve_max_depth() -> int:
    """The configured cascade depth cap (``FORGE_AUTOMATION_MAX_DEPTH``)."""
    raw = os.environ.get("FORGE_AUTOMATION_MAX_DEPTH")
    if not raw:
        return DEFAULT_MAX_DEPTH
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_DEPTH
    return value if value > 0 else DEFAULT_MAX_DEPTH


class LoopGuard:
    """Decides whether a rule may fire for an envelope, or must be skipped."""

    def __init__(self, max_depth: int | None = None) -> None:
        self.max_depth = max_depth if max_depth is not None else resolve_max_depth()

    def abort_reason(self, envelope: AutomationTriggerEnvelope, rule_id: uuid.UUID) -> str | None:
        """Return a reason string if the rule must be skipped, else ``None``."""
        if envelope.depth >= self.max_depth:
            return f"max_depth_reached:{self.max_depth}"
        if rule_id in envelope.causation_chain:
            return "self_cycle"
        return None

    def child_envelope(
        self, envelope: AutomationTriggerEnvelope, rule_id: uuid.UUID, **overrides: object
    ) -> AutomationTriggerEnvelope:
        """Build a child envelope (depth+1, causation extended) for a cascade."""
        return envelope.model_copy(
            update={
                "depth": envelope.depth + 1,
                "causation_chain": [*envelope.causation_chain, rule_id],
                **overrides,
            }
        )


__all__ = ["DEFAULT_MAX_DEPTH", "LoopGuard", "resolve_max_depth"]
