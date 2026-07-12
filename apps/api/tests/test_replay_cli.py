"""``forge-replay`` CLI tests — DB-backed cassette replay exit codes.

Mirrors ``test_cli_verify.py``'s convention: the DB-backed lookup runs against
a throwaway file-backed SQLite (in-memory ``sqlite://`` is not shared across
the CLI's own freshly-opened connection), exercised both a faithful replay
(exit 0) and a diverged one (exit 1), plus the "no database configured" park
(exit 3).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forge_agent.replay import RecordingModelClient, RecordingToolRegistry, RunCassette
from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry
from forge_api.cli_replay import main
from forge_contracts import AgentObjective
from forge_db.base import Base
from forge_db.models import RunRecording, Workspace


def _objective(text: str = "edit main") -> AgentObjective:
    return AgentObjective(objective=text, allowed_actions=["read_repo"])


def _record() -> RunCassette:
    cassette = RunCassette()
    model = RecordingModelClient(
        ScriptedModelClient(
            [
                tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
                finish_response("done", confidence=0.9),
            ]
        ),
        cassette,
    )
    tools = RecordingToolRegistry(ToolRegistry(), cassette)
    AgentRunner(model=model, tools=tools).run(_objective())
    return cassette


def _seed(url: str) -> uuid.UUID:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    cassette = _record()
    with Session(engine) as session:
        ws = Workspace(name="Acme", slug="acme")
        session.add(ws)
        session.flush()
        row = RunRecording(
            workspace_id=ws.id,
            cassette=cassette.to_dict(),
            model=cassette.llm_calls[-1].model,
            content_hash="a" * 64,
        )
        session.add(row)
        session.commit()
        recording_id = row.id
    engine.dispose()
    return recording_id


def _write_objective(tmp_path: Path, objective: AgentObjective) -> Path:
    path = tmp_path / "objective.json"
    path.write_text(json.dumps(objective.model_dump(mode="json")), encoding="utf-8")
    return path


def test_replay_matching_objective_reproduces(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    url = f"sqlite:///{tmp_path / 'replay.db'}"
    recording_id = _seed(url)
    objective_file = _write_objective(tmp_path, _objective())

    rc = main(
        [
            "--recording",
            str(recording_id),
            "--objective",
            str(objective_file),
            "--database-url",
            url,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "REPRODUCED" in out
    assert "llm#0: match" in out
    assert "tool#0 (read_file): match" in out


def test_replay_diverging_objective_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    url = f"sqlite:///{tmp_path / 'replay.db'}"
    recording_id = _seed(url)
    objective_file = _write_objective(tmp_path, _objective("a completely different objective"))

    rc = main(
        [
            "--recording",
            str(recording_id),
            "--objective",
            str(objective_file),
            "--database-url",
            url,
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "DIVERGED" in captured.out
    assert "diverged at llm#0" in captured.err


def test_replay_missing_recording_errors(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'replay.db'}"
    _seed(url)
    objective_file = _write_objective(tmp_path, _objective())

    rc = main(
        [
            "--recording",
            str(uuid.uuid4()),
            "--objective",
            str(objective_file),
            "--database-url",
            url,
        ]
    )
    assert rc == 1


def test_replay_without_database_url_parks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_DATABASE_URL", raising=False)
    objective_file = _write_objective(tmp_path, _objective())
    rc = main(["--recording", str(uuid.uuid4()), "--objective", str(objective_file)])
    assert rc == 3


def test_replay_invalid_objective_file_errors(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json", encoding="utf-8")
    rc = main(
        [
            "--recording",
            str(uuid.uuid4()),
            "--objective",
            str(bad_file),
            "--database-url",
            f"sqlite:///{tmp_path / 'unused.db'}",
        ]
    )
    assert rc == 1
