"""Map Adaptive Orchestration effort levels -> provider reasoning knobs (ao-effort).

Adaptive Orchestration configures a per-role *effort*
(:class:`forge_contracts.orchestration_config.Effort`:
``low``/``medium``/``high``/``max``) — how hard the model should think for that
role. Each BYOK provider exposes the depth control under a different name and a
different value domain, so the ao-config level must be translated per provider:

* **Anthropic (Claude)** — ``output_config.effort``. This is the current
  extended-thinking depth knob: the fixed ``thinking.budget_tokens`` budget is
  removed on Opus 4.7/4.8 (and 400s), replaced by ``effort``. Claude accepts
  ``low``/``medium``/``high``/``xhigh``/``max``, so the four AO levels map to
  themselves; ``xhigh`` (a valid Claude value operators may pin directly, but not
  an AO level) passes through too. Anything unrecognised falls back to ``high``
  (the Claude default) rather than risking a 400 on an invalid value.
* **OpenAI** — ``reasoning_effort``, which accepts ``low``/``medium``/``high``
  and has **no** ``max``; AO ``max`` (and Claude-only ``xhigh``) clamp to
  ``high``. ``reasoning_effort`` is only accepted by reasoning models (the
  ``o*`` / ``gpt-5`` families); sending it to a non-reasoning model (``gpt-4o``,
  ``gpt-4.1``) 400s, so :func:`openai_supports_reasoning_effort` gates it.

Pure, dependency-free lookups so the translators/adapters can apply them without
importing any provider SDK. ``Effort`` enum values and plain strings both work
(``Effort`` is a ``StrEnum``); matching is case-insensitive and whitespace-safe.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AO_EFFORT_LEVELS",
    "anthropic_effort",
    "openai_reasoning_effort",
    "openai_supports_reasoning_effort",
]

#: The Adaptive Orchestration effort levels, in ascending depth — the values of
#: :class:`forge_contracts.orchestration_config.Effort`.
AO_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")

#: The effort values Claude's ``output_config.effort`` accepts (Opus 4.7/4.8,
#: Sonnet 5). The AO levels are a subset; ``xhigh`` is Claude-only.
_ANTHROPIC_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

#: AO level (plus Claude-only ``xhigh``) -> OpenAI ``reasoning_effort``. OpenAI has
#: no ``max``, so ``max``/``xhigh`` clamp down to ``high``.
_OPENAI_EFFORT_BY_LEVEL: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

#: Fallback when an effort value is unrecognised — the provider-neutral default.
_DEFAULT_EFFORT = "high"

#: Prefixes of OpenAI model families that accept ``reasoning_effort``.
_OPENAI_REASONING_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")


def _key(effort: Any) -> str:
    return str(effort).strip().lower()


def anthropic_effort(effort: Any) -> str:
    """Map an AO effort level to Claude's ``output_config.effort`` value.

    The four AO levels (and Claude-only ``xhigh``) pass through; anything else
    falls back to ``high`` so an invalid value never reaches the API.
    """
    key = _key(effort)
    return key if key in _ANTHROPIC_EFFORTS else _DEFAULT_EFFORT


def openai_reasoning_effort(effort: Any) -> str:
    """Map an AO effort level to OpenAI's ``reasoning_effort`` value.

    ``max`` (and Claude-only ``xhigh``) clamp to ``high`` — OpenAI has no ``max``;
    unrecognised values fall back to ``high``.
    """
    return _OPENAI_EFFORT_BY_LEVEL.get(_key(effort), _DEFAULT_EFFORT)


def openai_supports_reasoning_effort(model: str) -> bool:
    """Whether ``model`` accepts ``reasoning_effort`` (the ``o*`` / ``gpt-5`` families).

    Non-reasoning models (``gpt-4o``, ``gpt-4.1``) reject the parameter with a
    400, so the OpenAI translator only sets it when this returns ``True``.
    """
    name = model.strip().lower()
    return any(
        name == prefix or name.startswith(f"{prefix}-") for prefix in _OPENAI_REASONING_PREFIXES
    )
