"""BYOK AI spec drafting (slice ``ss-draft`` — track: Spec Studio).

``POST /spec/draft`` asks the workspace's BYOK model — chosen by the
``ao-model-router`` and resolved through the existing HARD-02
:class:`~forge_contracts.ModelClient` — to draft a ``spec.md`` from a one-line
goal, with a spec-authoring system prompt **seeded with the project
constitution**. The draft is *streamed* (progressive assembly), then parsed to a
:class:`~forge_contracts.SpecManifest` *preview*. This is draft-only: nothing is
persisted; a human refines the result via the normal spec-editing endpoints.

Token/cost accounting rides the existing HARD-02 seam
(:class:`~forge_agent.providers.UsageAccumulator` + ``cost_usd``). The frozen
:class:`~forge_contracts.ModelStreamEvent` carries only text deltas (no usage),
so token counts for a streamed draft are *estimated* from the prompt and the
assembled draft and then priced through the very same cost table — the service
never reimplements the pricing logic.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from pydantic import BaseModel, Field

from forge_agent.providers import UsageAccumulator
from forge_contracts import (
    Constitution,
    ModelMessage,
    ModelRequest,
    SpecManifest,
    TokenUsage,
)
from forge_spec import SpecParseError, parse_spec_md

__all__ = [
    "DRAFT_PLACEHOLDER_ID",
    "SpecDraft",
    "build_draft_request",
    "build_system_prompt",
    "draft_spec",
    "estimate_tokens",
]

#: A draft has no real spec id yet (it is never persisted), so the model is told
#: to use this placeholder in the frontmatter; a human assigns the real id when
#: the draft is created for real via ``POST /spec/specs`` / ``PUT`` editing.
DRAFT_PLACEHOLDER_ID = "SPEC-DRAFT"

#: Default draft generation knobs (the injected client still owns provider-level
#: timeouts/retries; these only shape the request).
_DRAFT_MAX_TOKENS = 4000
_DRAFT_TEMPERATURE = 0.2

_BASE_INSTRUCTIONS = (
    "You are Forge's spec author. You turn a one-line engineering goal into a "
    "single, precise, testable specification following Spec-Driven Development. "
    "Write concrete, verifiable requirements and Given/When/Then acceptance "
    "criteria that each trace back to a requirement. Surface genuine ambiguity "
    "as open questions rather than inventing scope. Output ONLY the spec.md "
    "document — no preamble, no commentary, no code fences."
)

#: The exact ``spec.md`` serialization contract the parser
#: (:func:`forge_spec.parse_spec_md`) expects. Kept in lock-step with
#: :func:`forge_spec.render_spec_md`.
_SPEC_MD_CONTRACT = (
    "Emit the document in EXACTLY this format:\n\n"
    "---\n"
    f"id: {DRAFT_PLACEHOLDER_ID}\n"
    "status: draft\n"
    "---\n\n"
    "## Goal\n\n"
    "<one concise sentence naming what is being built>\n\n"
    "## Requirements\n\n"
    "- **R1**: <requirement>\n"
    "- **R2**: <requirement>\n\n"
    "## Acceptance Criteria\n\n"
    "- **A1** (R1): Given <context>, when <action>, then <observable outcome>\n\n"
    "## Constraints\n\n"
    "- <constraint>\n\n"
    "## Open Questions\n\n"
    "- **Q1**: <question that must be resolved before implementation>\n\n"
    "The YAML frontmatter block (between the '---' lines) and the '## Goal' "
    "section are mandatory; omit any other section that has no content."
)


class SpecDraft(BaseModel):
    """The draft-only result of ``POST /spec/draft`` (nothing is persisted)."""

    goal: str
    epic_id: uuid.UUID | None = None
    model: str
    spec_md: str
    #: The parsed preview, or ``None`` when the drafted markdown did not parse
    #: (``parse_error`` then explains why — the raw ``spec_md`` is still returned
    #: for the human to fix).
    manifest: SpecManifest | None = None
    parse_error: str | None = None
    #: The ``model_usage`` accounting artifact (input/output tokens + ``cost_usd``).
    usage: dict[str, Any] = Field(default_factory=dict)


def build_system_prompt(constitution: Constitution | None) -> str:
    """Build the spec-authoring system prompt, seeded with the constitution.

    When a ``constitution`` is available its principles and architecture
    guardrails are injected so the drafted spec conforms to the project's
    engineering constitution; otherwise the base authoring instructions and the
    ``spec.md`` format contract are used unchanged.
    """
    parts: list[str] = [_BASE_INSTRUCTIONS]
    if constitution is not None:
        if constitution.principles:
            bullet = "\n".join(f"- {p}" for p in constitution.principles)
            parts.append("Project constitution — principles:\n" + bullet)
        if constitution.architecture_guardrails:
            bullet = "\n".join(f"- {g}" for g in constitution.architecture_guardrails)
            parts.append("Project constitution — architecture guardrails:\n" + bullet)
    parts.append(_SPEC_MD_CONTRACT)
    return "\n\n".join(parts)


def build_draft_request(
    *,
    goal: str,
    model: str,
    system: str,
    epic_id: uuid.UUID | None = None,
) -> ModelRequest:
    """Build the streaming :class:`~forge_contracts.ModelRequest` for a draft."""
    user = f"Draft a spec.md for this engineering goal:\n\n{goal.strip()}"
    if epic_id is not None:
        user += f"\n\nThis spec belongs to epic {epic_id}."
    return ModelRequest(
        model=model,
        system=system,
        messages=[ModelMessage(role="user", content=user)],
        max_tokens=_DRAFT_MAX_TOKENS,
        temperature=_DRAFT_TEMPERATURE,
    )


def estimate_tokens(text: str) -> int:
    """Estimate tokens for ``text`` (~4 chars/token; a non-empty string is >= 1).

    Used only because the streaming contract surfaces no provider usage; the
    estimate is priced through the shared ``cost_usd`` table so accounting stays
    consistent with the rest of the platform.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _extract_spec_md(raw: str) -> str:
    """Best-effort clean-up of the streamed text into a parseable ``spec.md``.

    Strips an accidental Markdown code-fence wrapper and any prose the model
    emitted before the YAML frontmatter, so a slightly chatty model still yields
    a parseable document. A well-formed draft passes through unchanged.
    """
    text = raw.strip()
    if not text:
        return text
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]  # drop the opening ``` / ```markdown fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    marker = text.find("---")
    if marker > 0:
        text = text[marker:].strip()
    return text + "\n"


def draft_spec(
    client: Any,
    *,
    goal: str,
    model: str,
    constitution: Constitution | None = None,
    epic_id: uuid.UUID | None = None,
) -> SpecDraft:
    """Stream a spec draft from ``client`` and return the parsed preview + cost.

    ``client`` is any :class:`~forge_contracts.ModelClient` (a live BYOK client
    in production, a mock in tests). The draft is assembled from the streamed
    text deltas, parsed to a :class:`~forge_contracts.SpecManifest` preview
    (never persisted), and token/cost is recorded via
    :class:`~forge_agent.providers.UsageAccumulator`.
    """
    system = build_system_prompt(constitution)
    request = build_draft_request(goal=goal, model=model, system=system, epic_id=epic_id)

    chunks: list[str] = []
    for event in client.stream(request):
        piece = event.delta if event.delta is not None else event.text
        if piece:
            chunks.append(piece)
    raw = "".join(chunks)

    spec_md = _extract_spec_md(raw)
    manifest: SpecManifest | None = None
    parse_error: str | None = None
    try:
        manifest = parse_spec_md(spec_md)
    except SpecParseError as exc:
        parse_error = str(exc)

    prompt_text = (system or "") + "\n" + (request.messages[-1].content if request.messages else "")
    accumulator = UsageAccumulator()
    accumulator.add(
        TokenUsage(
            input_tokens=estimate_tokens(prompt_text),
            output_tokens=estimate_tokens(raw),
        )
    )

    return SpecDraft(
        goal=goal,
        epic_id=epic_id,
        model=model,
        spec_md=spec_md,
        manifest=manifest,
        parse_error=parse_error,
        usage=accumulator.to_artifact(model),
    )
