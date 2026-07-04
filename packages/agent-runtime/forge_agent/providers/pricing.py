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
#: Anthropic figures track the claude-api pricing table (Opus 4.x $5/$25, Sonnet
#: $3/$15, Haiku $1/$5, Fable 5 $10/$50). OpenAI figures are representative list
#: prices for the models an operator is most likely to select via
#: ``FORGE_MODEL_NAME``; an absent entry simply costs ``0.0`` (see ``cost_usd``).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # --- Anthropic (claude-api reference table) ---------------------------- #
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
    # --- OpenAI (representative list prices) ------------------------------- #
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
