"""Coordinator settings (env: ``MULTI_AGENT_*``).

Mirrors the deploy knobs in F27 §3.5. ``enabled`` defaults to ``False`` — a
supervised run refuses with ``multi_agent_disabled`` until an operator opts in.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

__all__ = ["CoordinatorSettings"]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class CoordinatorSettings(BaseModel):
    """Resolved coordinator configuration."""

    enabled: bool = False
    max_parallel_cap: int = 4
    subagent_timeout_seconds: int = 3600
    review_loop_budget: int = 1
    fallback_to_single_agent: bool = False
    confidence_threshold: float = 0.72

    extra: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls) -> CoordinatorSettings:
        """Build settings from ``MULTI_AGENT_*`` environment variables."""
        return cls(
            enabled=_env_bool("MULTI_AGENT_ENABLED", False),
            max_parallel_cap=_env_int("MULTI_AGENT_MAX_PARALLEL_CAP", 4),
            subagent_timeout_seconds=_env_int("MULTI_AGENT_SUBAGENT_TIMEOUT_SECONDS", 3600),
            review_loop_budget=_env_int("MULTI_AGENT_REVIEW_LOOP_BUDGET", 1),
            fallback_to_single_agent=_env_bool("MULTI_AGENT_FALLBACK_TO_SINGLE_AGENT", False),
            confidence_threshold=_env_float(
                "MULTI_AGENT_CONFIDENCE_THRESHOLD", 0.72
            ),
        )
