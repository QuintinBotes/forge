"""PMSyncEngine tests: create/update/no-op, echo suppression, conflict, direction.

Covers AC6, AC7, AC8, AC9, AC10, AC11, AC12, AC13, AC14, AC19, AC22.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    ConflictPolicy,
    ExternalTask,
    ForgePriority,
    ForgeTask,
    HealthResult,
    PMProvider,
    PMSyncState,
    StatusCategory,
    SyncDirection,
    WebhookEvent,
)
from forge_integrations.pm.hashing import external_content_hash, forge_content_hash
from forge_integrations.pm.sync_engine import (
    InMemoryAuditSink,
    InMemoryBoardWriter,
    InMemoryLinkRepository,
    LinkRecord,
    PMSyncEngine,
)

CONN = uuid4()
WS = uuid4()
PROJ = uuid4()


def _now() -> datetime:
    return datetime(2026, 1, 2, 10, 0, 0, tzinfo=UTC)


def _forge(**overrides) -> ForgeTask:
    base = {
        "id": uuid4(),
        "key": "TASK-1",
        "project_id": PROJ,
        "title": "Add pagination",
        "description_md": "body",
        "status_category": StatusCategory.started,
        "priority": ForgePriority.high,
        "assignee_email": "a@acme.test",
        "label_names": ["backend"],
        "version": 1,
        "updated_at": _now(),
    }
    base.update(overrides)
    return ForgeTask(**base)


def _external(**overrides) -> ExternalTask:
    base = {
        "provider": PMProvider.linear,
        "external_id": "ext-1",
        "external_key": "ENG-1",
        "url": "https://x/ENG-1",
        "title": "Add pagination",
        "description_md": "body",
        "status_name": "In Progress",
        "status_category": StatusCategory.started,
        "priority_token": "2",
        "assignee_email": "a@acme.test",
        "labels": ["backend"],
        "external_updated_at": _now(),
    }
    base.update(overrides)
    return ExternalTask(**base)


class FakeAdapter:
    """A controllable in-memory PMAdapter for engine tests."""

    provider = PMProvider.linear

    def __init__(self, *, clock=None) -> None:
        self.externals: dict[str, ExternalTask] = {}
        self.create_calls: list[ForgeTask] = []
        self.update_calls: list[tuple[str, ForgeTask]] = []
        self.fetch_calls: list[str] = []
        self._counter = 0
        self._clock = clock or _now

    # mapping — Linear-style 1:1
    def map_status(self, value: str, direction: Direction) -> str:
        return value

    def map_priority(self, value: str, direction: Direction) -> str:
        mapping = {"none": "0", "low": "4", "medium": "3", "high": "2", "urgent": "1"}
        if direction == Direction.OUT:
            return mapping[value]
        return {v: k for k, v in mapping.items()}[value]

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        return dict(fields)

    def _from_forge(self, forge_task: ForgeTask, external_id: str) -> ExternalTask:
        return ExternalTask(
            provider=self.provider,
            external_id=external_id,
            external_key=f"ENG-{external_id}",
            url=f"https://x/{external_id}",
            title=forge_task.title,
            description_md=forge_task.description_md,
            status_name=forge_task.status_category.value,
            status_category=forge_task.status_category,
            priority_token=self.map_priority(forge_task.priority.value, Direction.OUT),
            assignee_email=forge_task.assignee_email,
            labels=list(forge_task.label_names),
            external_updated_at=self._clock(),
        )

    async def fetch_external(self, external_id: str) -> ExternalTask:
        self.fetch_calls.append(external_id)
        return self.externals[external_id]

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        self.create_calls.append(forge_task)
        self._counter += 1
        ext = self._from_forge(forge_task, f"ext-{self._counter}")
        self.externals[ext.external_id] = ext
        return ext

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        self.update_calls.append((external_id, forge_task))
        ext = self._from_forge(forge_task, external_id)
        self.externals[external_id] = ext
        return ext

    async def list_external(self, *, cursor=None, limit=50):
        return list(self.externals.values()), None

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        raise NotImplementedError  # pragma: no cover

    def verify_webhook(self, body, headers, secret) -> bool:  # pragma: no cover
        return True

    async def register_webhook(self, callback_url: str, secret: str) -> str:  # pragma: no cover
        return "wh"

    async def unregister_webhook(self, external_webhook_id: str) -> None:  # pragma: no cover
        return None

    async def get_connection_health(self) -> HealthResult:  # pragma: no cover
        return HealthResult(status="connected", provider=self.provider)


def _engine(
    adapter: FakeAdapter,
    *,
    links: InMemoryLinkRepository | None = None,
    board: InMemoryBoardWriter | None = None,
    audit: InMemoryAuditSink | None = None,
    policy: ConflictPolicy = ConflictPolicy.newest_wins,
    direction: SyncDirection = SyncDirection.bidirectional,
) -> PMSyncEngine:
    return PMSyncEngine(
        adapter=adapter,
        links=links or InMemoryLinkRepository(),
        board=board or InMemoryBoardWriter(clock=_now),
        audit=audit or InMemoryAuditSink(),
        connection_id=CONN,
        workspace_id=WS,
        forge_project_id=PROJ,
        conflict_policy=policy,
        sync_direction=direction,
        clock=_now,
    )


# --- OUT create / update / no-op ------------------------------------------- #

async def test_sync_out_create_links_and_sets_watermarks() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    audit = InMemoryAuditSink()
    engine = _engine(adapter, links=links, audit=audit)
    task = _forge()

    outcome = await engine.sync_out(task)

    assert outcome.action == "created"
    assert len(adapter.create_calls) == 1
    link = links.get_by_forge_task(CONN, task.id)
    assert link is not None
    assert link.external_id == "ext-1"
    assert link.sync_state is PMSyncState.synced
    assert link.last_outbound_hash == forge_content_hash(task)
    assert link.last_inbound_hash is not None
    assert link.forge_version_at_sync == task.version
    assert len(audit.entries) == 1  # one outbound provider call audited (AC22)


async def test_sync_out_update_calls_update_external() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    engine = _engine(adapter, links=links)
    task = _forge()
    await engine.sync_out(task)

    changed = task.model_copy(update={"title": "New title", "version": 2})
    outcome = await engine.sync_out(changed)

    assert outcome.action == "updated"
    assert len(adapter.update_calls) == 1
    assert adapter.update_calls[0][0] == "ext-1"


async def test_sync_out_no_change_makes_no_external_call() -> None:
    adapter = FakeAdapter()
    engine = _engine(adapter)
    task = _forge()
    await engine.sync_out(task)
    adapter.create_calls.clear()

    outcome = await engine.sync_out(task)  # identical content

    assert outcome.action == "no_change"
    assert adapter.create_calls == []
    assert adapter.update_calls == []


# --- IN create / update ----------------------------------------------------- #

async def test_sync_in_create_via_board_service_tagged_pm_sync() -> None:
    adapter = FakeAdapter()
    board = InMemoryBoardWriter(clock=_now)
    links = InMemoryLinkRepository()
    engine = _engine(adapter, board=board, links=links)

    outcome = await engine.sync_in(_external())

    assert outcome.action == "created"
    assert len(board.created_sources) == 1
    assert board.created_sources[0]["source"] == "pm_sync"
    assert board.created_sources[0]["direction"] == "in"
    link = links.get_by_external(CONN, "ext-1")
    assert link is not None
    assert link.last_inbound_hash == external_content_hash(_external())


async def test_sync_in_update_writes_through_board() -> None:
    adapter = FakeAdapter()
    board = InMemoryBoardWriter(clock=_now)
    links = InMemoryLinkRepository()
    engine = _engine(adapter, board=board, links=links)
    await engine.sync_in(_external())

    changed = _external(title="Renamed")
    outcome = await engine.sync_in(changed)

    assert outcome.action == "updated"
    assert len(board.updated_sources) == 1
    assert board.updated_sources[0]["source"] == "pm_sync"


# --- Echo suppression (content hash) — AC12 -------------------------------- #

async def test_echo_suppression_out_after_in_write() -> None:
    adapter = FakeAdapter()
    board = InMemoryBoardWriter(clock=_now)
    links = InMemoryLinkRepository()
    engine = _engine(adapter, board=board, links=links)
    await engine.sync_in(_external())  # creates board task + link
    created = board.get(links.get_by_external(CONN, "ext-1").forge_task_id)

    # Now an OUT sync of that just-written task must be suppressed.
    outcome = await engine.sync_out(created)
    assert outcome.action == "no_change"
    assert adapter.update_calls == []
    assert adapter.create_calls == []


async def test_echo_suppression_in_after_out_write() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    board = InMemoryBoardWriter(clock=_now)
    engine = _engine(adapter, links=links, board=board)
    task = _forge()
    await engine.sync_out(task)  # creates external "ext-1" + link
    ext = adapter.externals["ext-1"]

    outcome = await engine.sync_in(ext)  # re-delivered identical external
    assert outcome.action == "skipped_echo"
    assert board.updated_sources == []


# --- Conflict resolution — AC13 / AC14 ------------------------------------- #

def _seed_conflict_link(links: InMemoryLinkRepository, forge_task: ForgeTask) -> LinkRecord:
    """A synced link whose stored hashes are 'stale' so both sides look changed."""
    link = LinkRecord(
        connection_id=CONN,
        workspace_id=WS,
        forge_task_id=forge_task.id,
        provider=PMProvider.linear,
        external_id="ext-1",
        external_key="ENG-1",
        external_url="https://x/ENG-1",
        last_outbound_hash="stale-out",
        last_inbound_hash="stale-in",
        forge_version_at_sync=1,
    )
    return links.upsert(link)


async def test_conflict_newest_wins_picks_forge() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    task = _forge(version=2, updated_at=_now() + timedelta(hours=1))
    _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(
        title="external edit", external_updated_at=_now()
    )
    engine = _engine(adapter, links=links, policy=ConflictPolicy.newest_wins)

    outcome = await engine.sync_out(task)

    assert outcome.action == "updated"
    assert outcome.winner == "forge"
    assert len(adapter.update_calls) == 1  # pushed forge OUT


async def test_conflict_newest_wins_picks_external() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    board = InMemoryBoardWriter(clock=_now)
    task = _forge(version=2, updated_at=_now())
    board.put(task)
    _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(
        title="external edit", external_updated_at=_now() + timedelta(hours=1)
    )
    engine = _engine(adapter, links=links, board=board, policy=ConflictPolicy.newest_wins)

    outcome = await engine.sync_out(task)

    assert outcome.action == "updated"
    assert outcome.winner == "external"
    assert adapter.update_calls == []  # external won -> no OUT write
    assert len(board.updated_sources) == 1  # applied IN


async def test_conflict_forge_wins_is_deterministic() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    task = _forge(updated_at=_now() - timedelta(days=1))  # older than external
    _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(external_updated_at=_now())
    engine = _engine(adapter, links=links, policy=ConflictPolicy.forge_wins)

    outcome = await engine.sync_out(task)
    assert outcome.winner == "forge"
    assert len(adapter.update_calls) == 1


async def test_conflict_external_wins_is_deterministic() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    board = InMemoryBoardWriter(clock=_now)
    task = _forge(updated_at=_now() + timedelta(days=1))  # newer than external
    board.put(task)
    _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(external_updated_at=_now())
    engine = _engine(adapter, links=links, board=board, policy=ConflictPolicy.external_wins)

    outcome = await engine.sync_out(task)
    assert outcome.winner == "external"
    assert adapter.update_calls == []


async def test_conflict_manual_no_write_sets_conflict_state() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    task = _forge()
    seeded = _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(title="external edit")
    engine = _engine(adapter, links=links, policy=ConflictPolicy.manual)

    outcome = await engine.sync_out(task)

    assert outcome.action == "conflict"
    assert adapter.update_calls == []
    link = links.get(seeded.id)
    assert link.sync_state is PMSyncState.conflict
    assert "forge" in link.conflict_detail
    assert "external" in link.conflict_detail


async def test_resolve_conflict_applies_winner_forge() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    task = _forge()
    seeded = _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(title="external edit")
    engine = _engine(adapter, links=links, policy=ConflictPolicy.manual)
    await engine.sync_out(task)

    outcome = await engine.resolve_conflict(seeded.id, "forge")
    assert outcome.winner == "forge"
    assert len(adapter.update_calls) == 1
    link = links.get(seeded.id)
    assert link.sync_state is PMSyncState.synced


async def test_resolve_conflict_applies_winner_external() -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    board = InMemoryBoardWriter(clock=_now)
    task = _forge()
    board.put(task)
    seeded = _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(title="external edit")
    engine = _engine(adapter, links=links, board=board, policy=ConflictPolicy.manual)
    await engine.sync_out(task)

    outcome = await engine.resolve_conflict(seeded.id, "external")
    assert outcome.winner == "external"
    assert len(board.updated_sources) == 1
    assert links.get(seeded.id).sync_state is PMSyncState.synced


# --- Direction enforcement — AC19 ------------------------------------------ #

async def test_sync_direction_inbound_only_skips_out() -> None:
    adapter = FakeAdapter()
    engine = _engine(adapter, direction=SyncDirection.inbound_only)
    outcome = await engine.sync_out(_forge())
    assert outcome.action == "no_change"
    assert outcome.detail.get("reason") == "inbound_only"
    assert adapter.create_calls == []


async def test_outbound_only_skips_in() -> None:
    adapter = FakeAdapter()
    board = InMemoryBoardWriter(clock=_now)
    engine = _engine(adapter, board=board, direction=SyncDirection.outbound_only)
    outcome = await engine.sync_in(_external())
    assert outcome.action == "skipped_echo"
    assert outcome.detail.get("reason") == "outbound_only"
    assert board.created_sources == []


@pytest.mark.parametrize("policy", list(ConflictPolicy))
async def test_all_conflict_policies_smoke(policy: ConflictPolicy) -> None:
    adapter = FakeAdapter()
    links = InMemoryLinkRepository()
    board = InMemoryBoardWriter(clock=_now)
    task = _forge()
    board.put(task)
    _seed_conflict_link(links, task)
    adapter.externals["ext-1"] = _external(title="x edit")
    engine = _engine(adapter, links=links, board=board, policy=policy)
    outcome = await engine.sync_out(task)
    assert outcome.action in {"updated", "conflict"}
