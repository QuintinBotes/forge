"""Per-model USD pricing + a derived ``cost_usd`` helper (HARD-02).

Cost is **derived and recorded outside the frozen contracts**: ``TokenUsage``
carries only ``input_tokens`` / ``output_tokens``; the USD amount and any
cache-read discount are computed here and surfaced via the run ``artifacts`` /
observability path — never added to the DTO.

The table maps ``model -> (input_usd_per_mtok, output_usd_per_mtok)``. An unknown
model yields ``0.0`` (cost is best-effort, never a hard failure), so a run
against a model the operator priced elsewhere still records a non-negative cost.
"""

from __future__ import annotations

from forge_contracts import TokenUsage

__all__ = ["MODEL_PRICING", "cost_usd"]

#: USD price per **million** tokens: ``model -> (input, output)``.
#:
#: Anthropic figures track the claude-api pricing table (cached 2026-10-06).
#: OpenAI GPT-6 figures are list prices reported by third-party trackers at the
#: October 2026 API launch, not read from OpenAI's own page; re-check before
#: relying on them for billing. An absent entry simply costs ``0.0`` (see
#: ``cost_usd``), which is also what a local model costs.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # --- Anthropic (claude-api reference table) ---------------------------- #
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    # --- OpenAI ------------------------------------------------------------ #
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "o4-mini": (1.1, 4.4),
    "o3": (2.0, 8.0),
}

#: Cache reads are billed at roughly a tenth of the base input rate (claude-api
#: prompt-caching economics); the discount lowers the reported cost.
_CACHE_READ_RATE = 0.1


def cost_usd(model: str, usage: TokenUsage | None, *, cache_read_tokens: int = 0) -> float:
    """Return the derived USD cost of ``usage`` for ``model``.

    ``input_tokens`` is the *uncached* remainder (Anthropic reports cache reads
    separately), so full-rate input tokens and ``cache_read_tokens`` (at ~0.1x)
    are priced independently. An unknown model or ``None`` usage yields ``0.0``.
    """
    if usage is None:
        return 0.0
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return 0.0
    input_rate, output_rate = pricing
    per_mtok = (
        usage.input_tokens * input_rate
        + cache_read_tokens * input_rate * _CACHE_READ_RATE
        + usage.output_tokens * output_rate
    )
    return per_mtok / 1_000_000.0
