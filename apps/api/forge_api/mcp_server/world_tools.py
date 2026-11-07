"""The world model over MCP (spec I §5).

Every read tool calls the same handler the REST router serves, so the MCP and REST
answers cannot drift; a domain error the handler raises as HTTP comes back as an
in-band tool error. ``world_propose`` is the only write tool, and it only ever
creates a proposal a person must accept (ADR-0005). Its provenance is ``mcp``: a
member's token used by an IDE agent is a machine acting for that member.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from forge_api.mcp_server.registry import McpTool, ToolContext, ToolError
from forge_api.routers import world as world_router
from forge_api.schemas.world import ProposalIn, ProposalOut
from forge_api.services.world_service import make_world_service
from forge_world import Layer, SourceKind

#: MCP pages are smaller than REST pages (spec I §4).
MAX_LIMIT = 100


class WorldSearchIn(BaseModel):
    query: str | None = Field(default=None, description="Name to search for")
    type: str | None = Field(default=None, description="Ontology class key")
    at: datetime | None = Field(default=None, description="World time (default now)")
    as_of: datetime | None = Field(default=None, description="Record time (default now)")
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT)


class WorldEntityIn(BaseModel):
    entity_id: uuid.UUID
    at: datetime | None = None
    as_of: datetime | None = None


class WorldFactsIn(BaseModel):
    subject_id: uuid.UUID | None = None
    predicate: str | None = Field(default=None, description="Relation key")
    object_id: uuid.UUID | None = None
    layer: Layer | None = None
    at: datetime | None = None
    as_of: datetime | None = None
    limit: int = Field(default=50, ge=1, le=MAX_LIMIT)


class WorldChangesIn(BaseModel):
    entity_id: uuid.UUID
    since: datetime | None = None
    until: datetime | None = None


class WorldConflictsIn(BaseModel):
    at: datetime | None = None


class WorldDecisionDriftIn(BaseModel):
    decision_id: uuid.UUID


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc


def _search(ctx: ToolContext, args: WorldSearchIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.search_entities(
            principal=ctx.principal,
            service=service,
            q=args.query,
            type=args.type,
            at=args.at,
            as_of=args.as_of,
            limit=args.limit,
            offset=0,
        )
    )


def _entity(ctx: ToolContext, args: WorldEntityIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.get_entity(
            principal=ctx.principal,
            service=service,
            entity_id=args.entity_id,
            at=args.at,
            as_of=args.as_of,
        )
    )


def _facts(ctx: ToolContext, args: WorldFactsIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.query_facts(
            principal=ctx.principal,
            service=service,
            subject=args.subject_id,
            predicate=args.predicate,
            object_id=args.object_id,
            layer=args.layer,
            at=args.at,
            as_of=args.as_of,
            limit=args.limit,
            offset=0,
        )
    )


def _changes(ctx: ToolContext, args: WorldChangesIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.get_entity_changes(
            principal=ctx.principal,
            service=service,
            entity_id=args.entity_id,
            since=args.since,
            until=args.until,
        )
    )


def _conflicts(ctx: ToolContext, args: WorldConflictsIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.list_conflicts(principal=ctx.principal, service=service, at=args.at)
    )


def _drift(ctx: ToolContext, args: WorldDecisionDriftIn) -> BaseModel:
    service = make_world_service(ctx.session)
    return _call(
        lambda: world_router.get_decision_drift(
            principal=ctx.principal, service=service, decision_id=args.decision_id
        )
    )


def _propose(ctx: ToolContext, args: ProposalIn) -> ProposalOut:
    service = make_world_service(ctx.session)

    def create() -> Any:
        with world_router._world_errors():
            return service.create_proposal(
                ctx.principal.workspace_id,
                args.to_input(),
                proposer_kind=SourceKind.MCP,
                proposer=world_router._actor(ctx.principal),
                rationale=args.rationale,
                verdict=args.verdict,
                confidence=args.gate_confidence,
            )

    proposal = _call(create)
    world_router._audit(
        service,
        ctx.principal,
        "proposal_created",
        target_type="world_proposal",
        target_id=proposal.id,
        details={
            "proposer_kind": proposal.proposer_kind.value,
            "phrase": world_router._clip(args.phrase),
            "predicate_key": args.predicate_key,
            "gate_decision": proposal.gate.get("decision"),
            "via": "mcp",
        },
    )
    service.session.commit()
    return world_router._proposal_out(proposal)


WORLD_TOOLS: tuple[McpTool, ...] = (
    McpTool(
        "world_search",
        "Find entities in forge's world model by name. `at` is world time, `as_of` record time.",
        WorldSearchIn,
        _search,
    ),
    McpTool(
        "world_entity",
        "One entity: its names and the facts holding at `at` as forge knew them at `as_of`, "
        "each with verbatim evidence.",
        WorldEntityIn,
        _entity,
    ),
    McpTool(
        "world_facts",
        "Facts filtered by subject, relation, object or layer, on both clocks, with evidence.",
        WorldFactsIn,
        _facts,
    ),
    McpTool(
        "world_changes",
        "What forge recorded or retracted about an entity between two record times.",
        WorldChangesIn,
        _changes,
    ),
    McpTool(
        "world_conflicts",
        "Contradictions: two values of a single-valued relation at once, unordered "
        "successions, and reasoning violations.",
        WorldConflictsIn,
        _conflicts,
    ),
    McpTool(
        "world_decision_drift",
        "Which facts a recorded decision rested on have since been corrected or retracted.",
        WorldDecisionDriftIn,
        _drift,
    ),
    McpTool(
        "world_propose",
        "Propose a fact. Never writes one: a person must accept the proposal in forge.",
        ProposalIn,
        _propose,
        write=True,
    ),
)

__all__ = ["MAX_LIMIT", "WORLD_TOOLS"]
