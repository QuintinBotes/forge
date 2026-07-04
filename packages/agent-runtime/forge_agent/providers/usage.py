"""Per-run token + cost aggregation (HARD-02).

``UsageAccumulator`` sums ``TokenUsage`` across the turns of a single agent run
and renders a serialisable ``model_usage`` artifact. Cost (USD) and cache-read
counts live here and on the open ``AgentRunResult.artifacts`` dict — never on the
frozen ``TokenUsage`` DTO.
"""

from __future__ import annotations

from typing import Any

from forge_agent.providers.pricing import cost_usd
from forge_contracts import TokenUsage

__all__ = ["UsageAccumulator"]


class UsageAccumulator:
    """Accumulates per-turn token usage into a per-run total + derived cost."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.calls = 0

    def add(self, usage: TokenUsage | None, *, cache_read_tokens: int = 0) -> None:
        """Fold one model turn's usage into the running total.

        A turn is always counted (``calls`` increments even when a provider
        returns no ``usage``), so ``calls`` equals the number of model turns.
        """
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
        self.cache_read_input_tokens += cache_read_tokens
        self.calls += 1

    def to_artifact(self, model: str) -> dict[str, Any]:
        """Render the ``artifacts['model_usage']`` payload for ``model``."""
        totals = TokenUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": cost_usd(model, totals, cache_read_tokens=self.cache_read_input_tokens),
            "calls": self.calls,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }
