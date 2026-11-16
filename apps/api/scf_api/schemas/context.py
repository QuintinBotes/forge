"""Request/response schemas for the ``/context`` router (spec J §2.1)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field

#: Bundle section names, in priority order (spec J §2.1).
SectionName = Literal["facts", "knowledge", "specs", "memories", "instructions"]

#: Item kinds a bundle can carry.
ItemKind = Literal["fact", "chunk", "spec", "memory", "instruction"]

#: Kinds recorded in ``agent_run.context_refs`` (spec J §2.5).
RefKind = Literal["fact", "chunk", "spec", "memory"]

#: Budget templates (spec J §2.1).
TemplateName = Literal["default", "review", "implementation", "research"]


class AssembleIn(BaseModel):
    """Body for ``POST /context/assemble`` (also the MCP tool's input)."""

    task: str = Field(min_length=1, max_length=20_000)
    budget_tokens: int = Field(default=8_000, ge=500, le=200_000)
    template: TemplateName = "default"
    agent_run_id: uuid.UUID | None = None
    entities: list[Annotated[str, Field(max_length=200)]] | None = Field(
        default=None, max_length=50
    )
    spec_ids: list[Annotated[str, Field(max_length=200)]] | None = Field(
        default=None, max_length=50
    )


class ContextItemOut(BaseModel):
    """One included bundle item; ``source`` labels its origin (ADR-0014)."""

    kind: ItemKind
    id: str
    source: str
    text: str
    tokens: int
    section: SectionName
    conflict: bool = False


class DroppedItemOut(BaseModel):
    """A candidate the bundle left out, with the reason."""

    kind: ItemKind
    id: str
    source: str
    reason: str
    tokens: int


class ContextRefOut(BaseModel):
    """One ``context_refs`` entry, as stored on the agent run (spec J §2.5)."""

    kind: RefKind
    id: str


class AssembleOut(BaseModel):
    """The token-budgeted bundle (spec J §2.1)."""

    template: TemplateName
    budget_tokens: int
    total_tokens: int
    #: Retrieval honesty envelope (ADR-0018), straight from the knowledge
    #: service: with no embedder these are ``["bm25"]`` / ``"unconfigured"``.
    legs: list[str]
    vector_leg: str
    rerank: str
    items: list[ContextItemOut]
    dropped: list[DroppedItemOut]
    context_refs: list[ContextRefOut]
    agent_run_id: uuid.UUID | None = None


__all__ = [
    "AssembleIn",
    "AssembleOut",
    "ContextItemOut",
    "ContextRefOut",
    "DroppedItemOut",
    "ItemKind",
    "RefKind",
    "SectionName",
    "TemplateName",
]
