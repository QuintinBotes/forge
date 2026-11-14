"""Context assembly (spec J §2.1/§2.5): one token-budgeted bundle per task.

Composes the world model, the knowledge index and the spec engine into a
single bundle of source-labelled items (ADR-0014) in this priority order:
world facts, knowledge passages, spec requirements, memories, and repo
instruction files. Templates set each section's share of the budget; an
unused share rolls forward to the next section. Requirement blocks are
atomic — a requirement that does not fit is dropped whole, never cut.

THE token estimate for the whole bundle lives here and nowhere else:
``tokens = ceil(len(text) / 4)`` (:func:`count_tokens`).

When the caller names an ``agent_run_id``, the ids actually included are
written to that run's ``context_refs`` (spec J §2.5); the GitHub PR webhook
later reads them back through :func:`record_pull_request_outcome` to write
outcome feedback. This module never writes a world fact (ADR-0005).
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forge_api.schemas.context import (
    AssembleOut,
    ContextItemOut,
    ContextRefOut,
    DroppedItemOut,
)
from forge_contracts import KnowledgeScope, RetrievedChunk
from forge_db.models import (
    AgentRun,
    KnowledgeSource,
    KnowledgeSourceKind,
    RetrievalChunk,
    RunStatus,
)
from forge_knowledge import KnowledgeService
from forge_spec import FileSpecEngine, SpecNotFoundError, is_spec_key, spec_id_for_key
from forge_world import (
    KNOWN_AS_KEY,
    EntityData,
    EvidenceData,
    FactData,
    FeedbackOutcome,
    Layer,
    NotFoundError,
    WorldModelService,
    WorldRepository,
)

#: Bundle sections in priority order (spec J §2.1).
SECTION_ORDER: tuple[str, ...] = ("facts", "knowledge", "specs", "memories", "instructions")

#: Per-template budget shares (percent), in SECTION_ORDER.
TEMPLATES: dict[str, tuple[int, int, int, int, int]] = {
    "default": (30, 35, 15, 10, 10),
    "review": (20, 45, 20, 5, 10),
    "implementation": (20, 40, 20, 5, 15),
    "research": (35, 45, 10, 10, 0),
}

#: Passages the knowledge leg may contribute before budgeting.
KNOWLEDGE_K = 20

#: Characters of the task sent to the knowledge search as its query.
MAX_QUERY_CHARS = 4_000

#: Page size when scanning every workspace entity for task mentions.
ENTITY_SCAN_LIMIT = 200

#: Distinct spec keys mentioned in prose that one request will look up.
MAX_SPEC_MENTIONS = 20

# Unbounded, a long hyphen-free run is rescanned from every position (quadratic in the
# task length). The lookbehind starts a match only at the head of a run and the bound
# caps each attempt; CodeQL's ReDoS check sees only the bound.
_SPEC_MENTION_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{0,63}-[0-9]+")

#: `<prefix>/run-<run_id.hex[:12]>` (the worker's run-branch naming).
_RUN_BRANCH_RE = re.compile(r"/run-([0-9a-f]{12})$")


class AgentRunNotFoundError(LookupError):
    """The named agent run does not exist in this workspace (HTTP 404)."""


class RunFinishedError(RuntimeError):
    """Refs cannot be written on a finished run (HTTP 409 ``run_finished``)."""

    code = "run_finished"


#: Alias spelling, mirroring :class:`AgentRunNotFoundError`.
AgentRunFinishedError = RunFinishedError


def count_tokens(text: str) -> int:
    """The bundle's one token estimate: ``ceil(len(text) / 4)``."""
    return math.ceil(len(text) / 4)


def is_instruction_path(path: str | None) -> bool:
    """True for repo instruction files: AGENTS.md, CLAUDE.md, .cursor/rules/*."""
    if not path:
        return False
    normalised = path.replace("\\", "/")
    basename = normalised.rsplit("/", 1)[-1]
    if basename in {"AGENTS.md", "CLAUDE.md"}:
        return True
    return "/.cursor/rules/" in f"/{normalised}"


@dataclass(frozen=True)
class _Candidate:
    kind: str
    id: str
    source: str
    text: str
    section: str
    ref_kind: str
    ref_id: str
    conflict: bool = False
    #: Connected conflict component key; members are budgeted atomically.
    conflict_group: str | None = None

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


def _all_entities(world: WorldModelService, workspace_id: uuid.UUID) -> list[EntityData]:
    """Every entity in the workspace, paged (never capped at one page)."""
    directory: list[EntityData] = []
    offset = 0
    while True:
        page = world.search_entities(workspace_id, q=None, limit=ENTITY_SCAN_LIMIT, offset=offset)
        directory.extend(page)
        if len(page) < ENTITY_SCAN_LIMIT:
            return directory
        offset += len(page)


def _known_as_names(
    world: WorldModelService, workspace_id: uuid.UUID
) -> dict[uuid.UUID, list[str]]:
    """Current ``known_as`` values by subject entity id (one paged read)."""
    names: dict[uuid.UUID, list[str]] = {}
    offset = 0
    page_size = 500
    while True:
        page = world.query_facts(
            workspace_id, predicate=KNOWN_AS_KEY, limit=page_size, offset=offset
        )
        for fact in page:
            if fact.value:
                names.setdefault(fact.subject_id, []).append(fact.value)
        if len(page) < page_size:
            return names
        offset += len(page)


def _name_mentioned(name: str, *, task: str, task_folded: str) -> bool:
    """Cheap substring prefilter, then a symbol-safe boundary regex.

    ``(?<!\\w)…(?!\\w)`` (not ``\\b``) so names that start or end with a
    non-word character (``C++``, ``.NET``) can still be detected, while
    ``Acme`` still does not match inside ``Acmeville``.
    """
    if not name.strip():
        return False
    if name.casefold() not in task_folded:
        return False
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", task, flags=re.IGNORECASE) is not None


def _resolve_entities(
    world: WorldModelService,
    workspace_id: uuid.UUID,
    *,
    task: str,
    entities: Sequence[str] | None,
) -> tuple[list[EntityData], dict[uuid.UUID, str]]:
    """Entities named by the caller or in the task, plus an id->label map."""
    found: dict[uuid.UUID, EntityData] = {}
    for raw in entities or ():
        name = raw.strip()
        if not name:
            continue
        try:
            entity_id = uuid.UUID(name)
        except ValueError:
            for entity in world.search_entities(workspace_id, q=name, limit=20):
                found.setdefault(entity.id, entity)
            continue
        try:
            profile = world.get_entity_profile(workspace_id, entity_id)
        except NotFoundError:
            continue  # foreign or missing ids simply never appear
        found.setdefault(profile.entity.id, profile.entity)
    directory = _all_entities(world, workspace_id)
    labels = {entity.id: entity.label for entity in directory}
    known_as = _known_as_names(world, workspace_id)
    task_folded = task.casefold()
    for entity in directory:
        if entity.id in found:
            continue
        names = [entity.label, *known_as.get(entity.id, [])]
        if any(_name_mentioned(name, task=task, task_folded=task_folded) for name in names):
            found[entity.id] = entity
    for entity in found.values():
        labels.setdefault(entity.id, entity.label)
    return list(found.values()), labels


def _fact_text(
    fact: FactData,
    labels: Mapping[uuid.UUID, str],
    evidence: Sequence[EvidenceData],
    *,
    effective: float,
    conflict_detail: str | None,
) -> str:
    subject = labels.get(fact.subject_id, str(fact.subject_id))
    if fact.value is not None:
        target = fact.value
    elif fact.object_id is not None:
        target = labels.get(fact.object_id, str(fact.object_id))
    else:  # pragma: no cover - the DB CHECK makes this unreachable
        target = ""
    lines = [
        f"{subject} {fact.phrase} {target} "
        f"[layer={fact.layer.value}, confidence={fact.confidence:.2f}, "
        f"effective_confidence={effective:.2f}]"
    ]
    lines.extend(f'Evidence: "{item.quote}" ({item.source_ref})' for item in evidence)
    if conflict_detail is not None:
        lines.append(f"CONFLICT: {conflict_detail} Both sides are shown.")
    return "\n".join(lines)


def _fact_candidates(
    world: WorldModelService,
    workspace_id: uuid.UUID,
    entities: Sequence[EntityData],
    labels: Mapping[uuid.UUID, str],
) -> tuple[list[_Candidate], list[_Candidate]]:
    """(fact, memory) candidates for the resolved entities, holding now."""
    if not entities:
        # No entity matched: no facts, and conflicts are not computed at all.
        return [], []
    facts_by_id: dict[uuid.UUID, FactData] = {}
    for entity in entities:
        profile = world.get_entity_profile(workspace_id, entity.id)
        for fact in (*profile.facts, *profile.object_of):
            if fact.predicate_key == KNOWN_AS_KEY:
                continue  # names identify entities; they are not bundle content
            facts_by_id.setdefault(fact.id, fact)
    facts = list(facts_by_id.values())
    evidence = world.evidence_for(workspace_id, [fact.id for fact in facts])
    effective = world.effective_confidences(workspace_id, facts)
    conflict_details: dict[uuid.UUID, str] = {}
    conflict_group_of: dict[uuid.UUID, str] = {}
    if facts:
        # Conflicts only for the resolved entities (never workspace-wide).
        conflicts = world.conflicts(workspace_id, entity_ids=[entity.id for entity in entities])
        parent: dict[uuid.UUID, uuid.UUID] = {fact_id: fact_id for fact_id in facts_by_id}

        def _find(fact_id: uuid.UUID) -> uuid.UUID:
            root = fact_id
            while parent[root] != root:
                root = parent[root]
            while parent[fact_id] != root:
                parent[fact_id], fact_id = root, parent[fact_id]
            return root

        for conflict in conflicts:
            present = [fid for fid in conflict.fact_ids if fid in facts_by_id]
            for fid in present:
                conflict_details.setdefault(fid, conflict.detail)
            for other in present[1:]:
                first_root, other_root = _find(present[0]), _find(other)
                if first_root != other_root:
                    parent[other_root] = first_root
        components: dict[uuid.UUID, list[uuid.UUID]] = {}
        for fid in facts_by_id:
            components.setdefault(_find(fid), []).append(fid)
        for members in components.values():
            if len(members) > 1:
                key = min(str(member) for member in members)
                for member in members:
                    conflict_group_of[member] = key

    fact_items: list[_Candidate] = []
    memory_items: list[tuple[float, _Candidate]] = []
    for fact in facts:
        fact_evidence = evidence.get(fact.id, [])
        source = (
            fact_evidence[0].source_ref
            if fact_evidence
            else f"world_model:{fact.source_kind.value}:{fact.actor}"
        )
        is_memory = fact.layer is Layer.MEMORY
        candidate = _Candidate(
            kind="memory" if is_memory else "fact",
            id=str(fact.id),
            source=source,
            text=_fact_text(
                fact,
                labels,
                fact_evidence,
                effective=effective[fact.id],
                conflict_detail=conflict_details.get(fact.id),
            ),
            section="memories" if is_memory else "facts",
            ref_kind="memory" if is_memory else "fact",
            ref_id=str(fact.id),
            conflict=fact.id in conflict_details,
            conflict_group=conflict_group_of.get(fact.id),
        )
        if is_memory:
            memory_items.append((effective[fact.id], candidate))
        else:
            fact_items.append(candidate)
    memory_items.sort(key=lambda pair: (-pair[0], pair[1].id))
    return fact_items, [candidate for _, candidate in memory_items]


def _chunk_source(chunk: RetrievedChunk) -> str:
    return chunk.source_uri or chunk.path or f"knowledge_source:{chunk.source_id}"


def _chunk_id(chunk: RetrievedChunk) -> str:
    if chunk.id:
        return chunk.id
    return f"{chunk.source_id}:{chunk.path}:{chunk.start_line}"


def _instruction_candidates(
    session: Session, workspace_id: uuid.UUID, *, seen_ids: set[str]
) -> list[_Candidate]:
    """Instruction files from ``repo`` knowledge sources only, by path.

    Anything else with an instruction-like path (a ``url``/``document``/
    ``mcp`` chunk named ``AGENTS.md``) is never an instruction; it stays a
    plain knowledge chunk (ADR-0014). Each item is labelled with its
    origin as ``"<source uri>:<path>"`` so policies can key on it.
    """
    path = RetrievalChunk.path
    rows = session.execute(
        select(RetrievalChunk, KnowledgeSource)
        .join(
            KnowledgeSource,
            RetrievalChunk.knowledge_source_id == KnowledgeSource.id,
        )
        .where(
            RetrievalChunk.workspace_id == workspace_id,
            KnowledgeSource.workspace_id == workspace_id,
            KnowledgeSource.kind == KnowledgeSourceKind.REPO,
            path.is_not(None),
            or_(
                path.ilike("%AGENTS.md"),
                path.ilike("%CLAUDE.md"),
                path.ilike("%.cursor/rules/%"),
            ),
        )
        .order_by(RetrievalChunk.path, RetrievalChunk.start_line, RetrievalChunk.id)
    ).all()
    candidates: list[_Candidate] = []
    for row, source_row in rows:
        if not is_instruction_path(row.path) or str(row.id) in seen_ids:
            continue
        seen_ids.add(str(row.id))
        candidates.append(
            _Candidate(
                kind="instruction",
                id=str(row.id),
                source=f"{source_row.uri}:{row.path}",
                text=row.content,
                section="instructions",
                ref_kind="chunk",
                ref_id=str(row.id),
            )
        )
    return candidates


def _spec_uuid(raw: str) -> uuid.UUID | None:
    """Resolve a spec uuid or human key (``SPEC-1``) to the engine's uuid."""
    try:
        return uuid.UUID(raw)
    except ValueError:
        pass
    if is_spec_key(raw):
        return spec_id_for_key(raw)
    if is_spec_key(raw.upper()):
        return spec_id_for_key(raw.upper())
    return None


def _spec_candidates(
    engine: FileSpecEngine | None,
    *,
    task: str,
    spec_ids: Sequence[str] | None,
) -> tuple[list[_Candidate], list[DroppedItemOut]]:
    """Requirement blocks (atomic) of specs named by id or mentioned in the task.

    Prose mentions are deduplicated and capped at ``MAX_SPEC_MENTIONS``
    (each lookup can scan the spec directory), and a spec already
    resolved — by uuid, so ``SPEC-1``/``spec-1``/its uuid are one — is
    skipped without a second lookup or a duplicate bundle entry.
    """
    caller_ids = [raw.strip() for raw in (spec_ids or ()) if raw.strip()]
    caller_set = set(caller_ids)
    mentions = list(
        dict.fromkeys(token.strip() for token in _SPEC_MENTION_RE.findall(task) if token.strip())
    )[:MAX_SPEC_MENTIONS]
    requested = list(dict.fromkeys([*caller_ids, *mentions]))
    candidates: list[_Candidate] = []
    dropped: list[DroppedItemOut] = []
    seen_specs: set[uuid.UUID] = set()
    for raw in requested:
        resolved = _spec_uuid(raw)
        if resolved is not None:
            if resolved in seen_specs:
                continue
            seen_specs.add(resolved)
        if engine is None or resolved is None:
            if engine is not None or spec_ids:
                dropped.append(
                    DroppedItemOut(
                        kind="spec",
                        id=raw,
                        source=f"spec:{raw}",
                        reason="spec_not_found" if engine is not None else "spec_unavailable",
                        tokens=0,
                    )
                )
            continue
        try:
            manifest = engine.read_manifest(resolved)
        except SpecNotFoundError:
            # A key-shaped token in prose is not necessarily a spec; only a
            # caller-supplied id earns a dropped entry.
            if raw in caller_set:
                dropped.append(
                    DroppedItemOut(
                        kind="spec", id=raw, source=f"spec:{raw}", reason="spec_not_found", tokens=0
                    )
                )
            continue
        status = getattr(manifest.status, "value", manifest.status)
        header = f"Spec {manifest.id} {manifest.name!r} (status: {status})"
        if not manifest.requirements:
            candidates.append(
                _Candidate(
                    kind="spec",
                    id=manifest.id,
                    source=f"spec:{manifest.id}",
                    text=header,
                    section="specs",
                    ref_kind="spec",
                    ref_id=manifest.id,
                )
            )
        for requirement in manifest.requirements:
            candidates.append(
                _Candidate(
                    kind="spec",
                    id=f"{manifest.id}:{requirement.id}",
                    source=f"spec:{manifest.id}",
                    text=f"{header} requirement {requirement.id}: {requirement.text}",
                    section="specs",
                    ref_kind="spec",
                    ref_id=manifest.id,
                )
            )
    return candidates, dropped


def _dropped_for(candidate: _Candidate, *, budget_tokens: int) -> DroppedItemOut:
    return DroppedItemOut(
        kind=candidate.kind,  # type: ignore[arg-type]
        id=candidate.id,
        source=candidate.source,
        reason=("item_exceeds_budget" if candidate.tokens > budget_tokens else "budget_exceeded"),
        tokens=candidate.tokens,
    )


def _apply_budget(
    by_section: Mapping[str, Sequence[_Candidate]], *, budget_tokens: int, template: str
) -> tuple[list[_Candidate], list[DroppedItemOut], int]:
    """Greedily include candidates within per-section shares + roll-forward.

    A conflict group is all-or-nothing: its tokens are reserved (or the
    whole group dropped) at its first member, so the bundle never shows
    one side of a conflict while claiming both sides are shown. A group
    split across sections is reconciled the same way at the end.
    """
    included: list[_Candidate] = []
    dropped: list[DroppedItemOut] = []
    total = 0
    carry = 0
    for section, percent in zip(SECTION_ORDER, TEMPLATES[template], strict=True):
        # A 0% share excludes the section entirely (no roll-in either);
        # the carry passes through to the next section untouched.
        available = 0 if percent == 0 else (budget_tokens * percent // 100) + carry
        used = 0
        candidates = list(by_section.get(section, ()))
        group_fits: dict[str, bool] = {}
        for candidate in candidates:
            group = candidate.conflict_group
            if group is None:
                if (
                    candidate.tokens <= available - used
                    and total + candidate.tokens <= budget_tokens
                ):
                    included.append(candidate)
                    used += candidate.tokens
                    total += candidate.tokens
                else:
                    dropped.append(_dropped_for(candidate, budget_tokens=budget_tokens))
                continue
            if group not in group_fits:
                group_tokens = sum(
                    member.tokens for member in candidates if member.conflict_group == group
                )
                fits = group_tokens <= available - used and total + group_tokens <= budget_tokens
                group_fits[group] = fits
                if fits:
                    # Reserve the whole group now; members append as met.
                    used += group_tokens
                    total += group_tokens
            if group_fits[group]:
                included.append(candidate)
            else:
                dropped.append(_dropped_for(candidate, budget_tokens=budget_tokens))
        carry = carry if percent == 0 else available - used
    groups: dict[str, list[_Candidate]] = {}
    for section_candidates in by_section.values():
        for candidate in section_candidates:
            if candidate.conflict_group is not None:
                groups.setdefault(candidate.conflict_group, []).append(candidate)
    if groups:
        included_objects = {id(candidate) for candidate in included}
        doomed = [
            member
            for members in groups.values()
            if 0 < sum(id(member) in included_objects for member in members) < len(members)
            for member in members
            if id(member) in included_objects
        ]
        if doomed:
            doomed_objects = {id(candidate) for candidate in doomed}
            included = [c for c in included if id(c) not in doomed_objects]
            for candidate in doomed:
                total -= candidate.tokens
                dropped.append(_dropped_for(candidate, budget_tokens=budget_tokens))
    return included, dropped, total


def assemble_context(
    *,
    session: Session,
    world: WorldModelService,
    knowledge: KnowledgeService,
    spec_engine: FileSpecEngine | None,
    workspace_id: uuid.UUID,
    task: str,
    budget_tokens: int = 8_000,
    template: str = "default",
    agent_run_id: uuid.UUID | None = None,
    entities: Sequence[str] | None = None,
    spec_ids: Sequence[str] | None = None,
) -> AssembleOut:
    """Build the bundle; with ``agent_run_id``, record ``context_refs`` on it.

    Workspace-scoped throughout: a foreign run raises
    :class:`AgentRunNotFoundError`; foreign entities, facts and chunks
    never appear. The caller owns the transaction (this flushes the
    ``context_refs`` write, never commits). Refs are frozen at finish:
    writing them on a run that is not PENDING/RUNNING raises
    :class:`RunFinishedError` (409 ``run_finished``).
    """
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    run: AgentRun | None = None
    if agent_run_id is not None:
        run = session.get(AgentRun, agent_run_id)
        if run is None or run.workspace_id != workspace_id:
            raise AgentRunNotFoundError(f"no agent run {agent_run_id}")
        if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
            raise RunFinishedError(
                f"run_finished: agent run {agent_run_id} is {run.status}; context_refs are frozen"
            )

    resolved_entities, labels = _resolve_entities(world, workspace_id, task=task, entities=entities)
    fact_items, memory_items = _fact_candidates(world, workspace_id, resolved_entities, labels)

    # Instructions come only from repo sources (found by path, labelled
    # with their origin). A search hit is an instruction only when its id
    # is in that set — and then the path-found item already carries it, so
    # the hit is skipped; anything else with an instruction-like path
    # stays a plain chunk in the knowledge section.
    deduped_instructions = _instruction_candidates(session, workspace_id, seen_ids=set())
    instruction_ids = {candidate.id for candidate in deduped_instructions}
    scope = KnowledgeScope(workspace_id=workspace_id)
    retrieved = knowledge.search(task[:MAX_QUERY_CHARS], scope, k=KNOWLEDGE_K)
    knowledge_items: list[_Candidate] = []
    for chunk in retrieved:
        chunk_id = _chunk_id(chunk)
        if chunk_id in instruction_ids:
            continue
        knowledge_items.append(
            _Candidate(
                kind="chunk",
                id=chunk_id,
                source=_chunk_source(chunk),
                text=chunk.content,
                section="knowledge",
                ref_kind="chunk",
                ref_id=chunk_id,
            )
        )

    spec_items, spec_dropped = _spec_candidates(spec_engine, task=task, spec_ids=spec_ids)

    included, dropped, total = _apply_budget(
        {
            "facts": fact_items,
            "knowledge": knowledge_items,
            "specs": spec_items,
            "memories": memory_items,
            "instructions": deduped_instructions,
        },
        budget_tokens=budget_tokens,
        template=template,
    )
    dropped = [*spec_dropped, *dropped]

    refs: list[ContextRefOut] = []
    seen_refs: set[tuple[str, str]] = set()
    for candidate in included:
        key = (candidate.ref_kind, candidate.ref_id)
        if key not in seen_refs:
            seen_refs.add(key)
            refs.append(ContextRefOut(kind=candidate.ref_kind, id=candidate.ref_id))  # type: ignore[arg-type]

    if run is not None:
        run.context_refs = [{"kind": ref.kind, "id": ref.id} for ref in refs]
        session.flush()

    rerank = knowledge.rerank_status
    if rerank != "off" and retrieved and all(c.rerank_score is None for c in retrieved):
        rerank = "fallback"
    return AssembleOut(
        template=template,  # type: ignore[arg-type]
        budget_tokens=budget_tokens,
        total_tokens=total,
        legs=knowledge.legs,
        vector_leg=knowledge.vector_leg,
        rerank=rerank,
        items=[
            ContextItemOut(
                kind=candidate.kind,  # type: ignore[arg-type]
                id=candidate.id,
                source=candidate.source,
                text=candidate.text,
                tokens=candidate.tokens,
                section=candidate.section,  # type: ignore[arg-type]
                conflict=candidate.conflict,
            )
            for candidate in included
        ],
        dropped=dropped,
        context_refs=refs,
        agent_run_id=agent_run_id,
    )


# --------------------------------------------------------------------------- #
# PR outcome feedback (the GitHub webhook trigger, spec J §2.5)               #
# --------------------------------------------------------------------------- #


def _recorded_pr(run: AgentRun) -> Mapping[str, Any]:
    output = run.output if isinstance(run.output, Mapping) else {}
    artifacts = output.get("artifacts") if isinstance(output, Mapping) else None
    pull_request = artifacts.get("pull_request") if isinstance(artifacts, Mapping) else None
    return pull_request if isinstance(pull_request, Mapping) else {}


def _run_repo(run: AgentRun) -> str | None:
    inputs = run.inputs if isinstance(run.inputs, Mapping) else {}
    context = inputs.get("context") if isinstance(inputs, Mapping) else None
    repo = context.get("repo_full_name") if isinstance(context, Mapping) else None
    if isinstance(repo, str) and repo:
        return repo
    url = _recorded_pr(run).get("url")
    if isinstance(url, str):
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/", url)
        if match:
            return match.group(1)
    return None


def _effective_context_refs(run: AgentRun) -> Sequence[Any]:
    """The refs feedback must use: frozen artifacts first, else the column.

    The worker copies ``context_refs`` into ``output.artifacts`` when it
    records the outcome, freezing what the run actually consumed; that
    copy wins whenever it is present.
    """
    output = run.output if isinstance(run.output, Mapping) else {}
    artifacts = output.get("artifacts") if isinstance(output, Mapping) else None
    frozen = artifacts.get("context_refs") if isinstance(artifacts, Mapping) else None
    if isinstance(frozen, (list, tuple)):
        return frozen
    return run.context_refs or ()


def resolve_run_for_pull_request(
    session: Session,
    *,
    repo_full_name: str | None,
    number: int | None,
    head_ref: str | None,
) -> AgentRun | None:
    """The agent run whose recorded PR this is, else ``None`` (ignored).

    Primary: recorded PR number **and** the run's repository equal to
    the delivery's repository, filtered in SQL, newest first, accepted
    only when exactly one row matches. Nothing matches when the
    delivery has no repository. Fallback: a head branch ending
    ``/run-<12 hex>`` matched against run-id prefixes, accepted only
    when the single match's repository matches the delivery's and its
    recorded PR number is absent or equal — a spoofed or fork branch
    naming another run (run ids leak via public branch names) can never
    write that run's one-shot feedback first. Callers additionally
    withhold ``head_ref`` for fork PRs (head repo ≠ base repo).
    """
    if not repo_full_name:
        return None
    if number is not None:
        recorded = AgentRun.output["artifacts"]["pull_request"]["number"].as_integer()
        run_repo = AgentRun.inputs["context"]["repo_full_name"].as_string()
        rows = session.scalars(
            select(AgentRun)
            .where(recorded == number, run_repo == repo_full_name)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(2)
        ).all()
        if len(rows) == 1:
            return rows[0]
        # Zero or ambiguous (≥2): primary accepts nothing; the branch
        # fallback below may still identify one specific run.
    if head_ref:
        match = _RUN_BRANCH_RE.search(head_ref)
        if match:
            # Run branches carry the first 12 hex digits of the run id: a range
            # over the uuid column finds it on both dialects without a scan.
            prefix = match.group(1)
            low = uuid.UUID(prefix + "0" * 20)
            high = uuid.UUID(prefix + "f" * 20)
            found = session.scalars(
                select(AgentRun).where(AgentRun.id >= low, AgentRun.id <= high).limit(2)
            ).all()
            if len(found) == 1:
                run = found[0]
                recorded_number = _recorded_pr(run).get("number")
                if _run_repo(run) != repo_full_name:
                    return None
                if recorded_number is not None and recorded_number != number:
                    return None
                return run
    return None


def record_pull_request_outcome(
    session: Session,
    *,
    repo_full_name: str | None,
    number: int | None,
    merged: bool,
    head_ref: str | None,
) -> int:
    """Reinforce/weaken every fact a closed PR's run consumed; rows written.

    Idempotent per ``(run_id, fact_id)`` — a replay returns 0. A PR that
    maps to no run is ignored (0). Commits the session: the webhook owns
    the transaction for this side effect and nothing else.
    """
    run = resolve_run_for_pull_request(
        session, repo_full_name=repo_full_name, number=number, head_ref=head_ref
    )
    if run is None:
        return 0
    fact_ids: list[uuid.UUID] = []
    for ref in _effective_context_refs(run):
        if not isinstance(ref, Mapping) or ref.get("kind") not in ("fact", "memory"):
            continue
        try:
            fact_ids.append(uuid.UUID(str(ref.get("id"))))
        except ValueError:
            continue
    if not fact_ids:
        return 0
    world = WorldModelService(WorldRepository(session))
    written = world.record_feedback(
        run.workspace_id,
        run_id=run.id,
        fact_ids=fact_ids,
        outcome=FeedbackOutcome.REINFORCED if merged else FeedbackOutcome.WEAKENED,
    )
    session.commit()
    return written


__all__ = [
    "ENTITY_SCAN_LIMIT",
    "KNOWLEDGE_K",
    "MAX_SPEC_MENTIONS",
    "SECTION_ORDER",
    "TEMPLATES",
    "AgentRunFinishedError",
    "AgentRunNotFoundError",
    "RunFinishedError",
    "assemble_context",
    "count_tokens",
    "is_instruction_path",
    "record_pull_request_outcome",
    "resolve_run_for_pull_request",
]
