"""``forge replay`` CLI (Time-Travel Runs) — DB-backed cassette replay.

Loads a persisted ``RunRecording`` cassette from the configured database,
reconstructs it (``RunCassette.from_dict``), and re-runs the supplied
objective through a fresh ``AgentRunner`` wired with the replay-by-
substitution wrappers (``forge_agent.replay`` — never a live model or tool).
Prints a step-by-step diff against the tape and exits non-zero when the
replay diverged, mirroring the DB-backed ``forge-verify --run`` precedent
(``cli_verify.py``) and the offline ``forge bench verify`` exit-code
convention (``cli_bench.py``): ``0`` reproduced, ``1`` diverged/error, ``3``
no database configured (a "can't check" distinct from "checked and it
diverged").

Runnable as ``python -m forge_api.cli_replay`` (wired as the ``forge-replay``
console script).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from forge_agent.replay import RunCassette
from forge_contracts import AgentObjective

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_db.models import RunRecording

__all__ = ["build_parser", "main"]


def _read_source(path: str) -> str:
    """Read ``path`` as UTF-8, or stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _session_factory(database_url: str | None) -> sessionmaker[Session] | None:
    import os

    from forge_db.session import create_db_engine, create_session_factory

    url = database_url or os.environ.get("FORGE_DATABASE_URL")
    if not url:
        print(
            "no database configured: pass --database-url or set FORGE_DATABASE_URL",
            file=sys.stderr,
        )
        return None
    return create_session_factory(create_db_engine(url))


def _load_recording(
    session: Session, recording_id: uuid.UUID, workspace: str | None
) -> RunRecording | None:
    from sqlalchemy import select

    from forge_db.models import RunRecording

    query = select(RunRecording).where(RunRecording.id == recording_id)
    if workspace:
        query = query.where(RunRecording.workspace_id == uuid.UUID(workspace))
    return session.scalars(query).first()


def _cmd_replay(args: argparse.Namespace) -> int:
    from forge_api.services.replay_service import replay_recording

    try:
        recording_id = uuid.UUID(args.recording)
    except ValueError:
        print(f"error: --recording must be a UUID: {args.recording}", file=sys.stderr)
        return 1

    try:
        raw_objective = json.loads(_read_source(args.objective))
        objective = AgentObjective.model_validate(raw_objective)
    except (OSError, ValueError) as exc:
        print(f"error: invalid --objective: {exc}", file=sys.stderr)
        return 1

    factory = _session_factory(args.database_url)
    if factory is None:
        return 3

    with factory() as session:
        row = _load_recording(session, recording_id, args.workspace)
        if row is None:
            print(f"error: no run recording {recording_id}", file=sys.stderr)
            return 1
        cassette = RunCassette.from_dict(row.cassette)

    outcome = replay_recording(cassette, objective, max_iterations=args.max_iterations)

    for step in outcome.steps:
        label = f"{step.boundary}#{step.index}" + (f" ({step.name})" if step.name else "")
        print(f"{label}: {'match' if step.matched else 'MISMATCH'}")
    if outcome.divergence is not None:
        d = outcome.divergence
        where = f"{d.boundary}#{d.index}" + (f" ({d.name})" if d.name else "")
        print(f"diverged at {where}: expected={d.expected} actual={d.actual}", file=sys.stderr)
    elif outcome.diverged:
        print("diverged: replay finished without consuming the full tape", file=sys.stderr)

    print("DIVERGED" if outcome.diverged else "REPRODUCED")
    return 1 if outcome.diverged else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge-replay",
        description=(
            "Time-Travel Runs: replay a persisted RunRecording cassette by "
            "substitution and diff it against the tape."
        ),
    )
    parser.add_argument("--recording", required=True, metavar="ID", help="RunRecording id (UUID)")
    parser.add_argument(
        "--objective",
        required=True,
        metavar="FILE",
        help="the AgentObjective JSON that produced the recording ('-' for stdin)",
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        help="database URL to load the recording from (else FORGE_DATABASE_URL)",
    )
    parser.add_argument("--workspace", help="workspace UUID to scope the lookup")
    parser.add_argument("--max-iterations", dest="max_iterations", type=int, default=12)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _cmd_replay(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
