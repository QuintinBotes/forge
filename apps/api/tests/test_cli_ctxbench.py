"""``scf-cli ctxbench`` end to end, hermetically (spec J §2.4.1).

``build`` and ``proxy`` ingest the shipped suite's committed corpus (an
invented project, Quayline) into fresh SQLite databases (and fresh Postgres
schemas when ``FORGE_TEST_DATABASE_URL`` is set), with no network and no
model. ``record`` reads a fake GitHub page client and clones a tiny synthetic
git repository (invented content, assembled at test time with fixed dates so
its shas are stable). ``run`` and ``grade`` call the fake ``claude``
executable in ``packages/evaluation/tests/ctxbench`` over synthetic suites,
never a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from scf_api import cli, cli_ctxbench
from scf_db.base import Base
from scf_db.session import create_session_factory
from scf_eval.ctxbench.bank import SUITE_DIR, suite_hash

ROOT = Path(__file__).resolve().parents[3]
FAKE_CLAUDE = str(ROOT / "packages" / "evaluation" / "tests" / "ctxbench" / "fake_claude.py")
SUBJECT = "acme-example/widgets"
CUTOFF = "2026-10-27T00:00:00Z"
TOKEN = "fcb-test-viewer-token"

# --------------------------------------------------------------------------- #
# the synthetic corpus (invented)                                              #
# --------------------------------------------------------------------------- #

ADR = """# ADR-001: One table for the widget queue

Status: Accepted

## Decision

The widget queue keeps every pending widget in the single Postgres table
`widget_jobs`, because a second broker would double the moving parts.
"""
QUEUE = '''"""The widget queue."""


def enqueue_widget(name: str, priority: int = 0) -> str:
    """Put one widget on the queue and return its job id."""
    return f"{priority}:{name}"
'''
BLINKER = "def blink() -> None:\n    pass\n"
CLAUDE_MD = """# Notes for agents

Warm the sprocket cache before the first request: the cold path gives up
after 3 seconds and every widget request then fails.
"""
COMMITS: list[tuple[str, str, dict[str, str | None]]] = [
    (
        "2026-09-01T10:00:00+00:00",
        "Start the widget service",
        {
            "README.md": "# Widgets\n\nA small service that queues widgets.\n",
            "docs/adr/ADR-001-queue.md": ADR,
            "src/widgets/queue.py": QUEUE,
            "src/widgets/blinker.py": BLINKER,
        },
    ),
    ("2026-09-15T09:30:00+00:00", "Agent notes on the sprocket cache", {"CLAUDE.md": CLAUDE_MD}),
    (
        "2026-10-01T16:45:00+00:00",
        "Drop the legacy blinker (#7)",
        {
            "src/widgets/blinker.py": None,
            "README.md": "# Widgets\n\nA small service that queues widgets. No more blinker.\n",
        },
    ),
]


def _user(login: str) -> dict[str, str]:
    return {"login": login}


ISSUES = [
    {
        "number": 3,
        "title": "Sprockets jam when the queue is full",
        "state": "open",
        "body": "With 500 pending widgets the sprocket feeder stalls.",
        "user": _user("dana"),
        "labels": [{"name": "bug"}],
        "created_at": "2026-09-02T08:00:00Z",
        "closed_at": None,
    },
    {
        "number": 5,
        "title": "Retire the blinker",
        "state": "closed",
        "body": "Nobody reads the blink signal any more.",
        "user": _user("lee"),
        "labels": [],
        "created_at": "2026-09-10T12:00:00Z",
        "closed_at": "2026-10-01T17:00:00Z",
    },
    {  # the issues listing includes a stub for every PR
        "number": 7,
        "title": "Drop the legacy blinker",
        "state": "closed",
        "body": "Closes #5.",
        "user": _user("lee"),
        "labels": [],
        "created_at": "2026-09-30T10:00:00Z",
        "closed_at": "2026-10-01T16:45:00Z",
        "pull_request": {"url": "https://api.github.invalid/pulls/7"},
    },
    {  # created after the cut-off: never in the corpus
        "number": 9,
        "title": "Gizmo counter overflows",
        "state": "open",
        "body": "The gizmo counter wraps at 255.",
        "user": _user("dana"),
        "labels": [],
        "created_at": "2026-10-28T08:00:00Z",
        "closed_at": None,
    },
    *(
        {
            "number": number,
            "title": "a PR stub",
            "state": "closed",
            "body": "",
            "user": _user("lee"),
            "labels": [],
            "created_at": created,
            "closed_at": "2026-10-30T10:00:00Z",
            "pull_request": {"url": f"https://api.github.invalid/pulls/{number}"},
        }
        for number, created in ((8, "2026-10-20T10:00:00Z"), (10, "2026-10-29T10:00:00Z"))
    ),
]
PULLS = [
    {
        "number": 7,
        "title": "Drop the legacy blinker",
        "state": "closed",
        "body": "Closes #5.",
        "user": _user("lee"),
        "labels": [],
        "created_at": "2026-09-30T10:00:00Z",
        "closed_at": "2026-10-01T16:45:00Z",
        "merged_at": "2026-10-01T16:45:00Z",
        "merge_commit_sha": "0" * 40,
    },
    {  # merged after the cut-off: open as the corpus stood
        "number": 8,
        "title": "Count the gizmos",
        "state": "closed",
        "body": "Adds a gizmo counter.",
        "user": _user("lee"),
        "labels": [],
        "created_at": "2026-10-20T10:00:00Z",
        "closed_at": "2026-10-28T12:00:00Z",
        "merged_at": "2026-10-28T12:00:00Z",
        "merge_commit_sha": "1" * 40,
    },
    {  # created after the cut-off: never in the corpus
        "number": 10,
        "title": "Widen the gizmo counter",
        "state": "closed",
        "body": "Fixes #9.",
        "user": _user("lee"),
        "labels": [],
        "created_at": "2026-10-29T10:00:00Z",
        "closed_at": "2026-10-30T10:00:00Z",
        "merged_at": "2026-10-30T10:00:00Z",
        "merge_commit_sha": "2" * 40,
    },
]
ISSUE_COMMENTS = [
    {
        "issue_url": "https://api.github.invalid/repos/x/issues/3",
        "body": "Seen again on Tuesday.",
        "user": _user("sam"),
        "created_at": "2026-09-03T09:00:00Z",
    },
    {  # after the cut-off: never in the corpus
        "issue_url": "https://api.github.invalid/repos/x/issues/3",
        "body": "Fixed by raising the feeder limit to 900.",
        "user": _user("dana"),
        "created_at": "2026-10-28T09:00:00Z",
    },
]
REVIEW_COMMENTS = [
    {
        "pull_request_url": "https://api.github.invalid/repos/x/pulls/7",
        "path": "src/widgets/blinker.py",
        "body": "Is anything still importing blink()?",
        "user": _user("sam"),
        "created_at": "2026-09-30T11:00:00Z",
    },
    {  # after the cut-off: never in the corpus
        "pull_request_url": "https://api.github.invalid/repos/x/pulls/8",
        "path": "src/widgets/queue.py",
        "body": "Counted twice on retry; fine to land.",
        "user": _user("sam"),
        "created_at": "2026-10-28T11:00:00Z",
    },
]
LISTINGS = {
    "issues": ISSUES,
    "pulls": PULLS,
    "issues/comments": ISSUE_COMMENTS,
    "pulls/comments": REVIEW_COMMENTS,
}


class FakeGitHub:
    """``iter_pages`` over the listings above; records every call."""

    def __init__(self, listings: Mapping[str, list[dict[str, Any]]] = LISTINGS) -> None:
        self.listings = listings
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def iter_pages(
        self, repo: str, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((repo, path, dict(params or {})))
        return iter([dict(item) for item in self.listings[path]])


def _make_repo(
    root: Path, commits: list[tuple[str, str, dict[str, str | None]]] = COMMITS
) -> list[str]:
    """The commits, with fixed authors and dates; returns their shas."""
    root.mkdir(parents=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Synthetic Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
    }

    def git(*args: str, date: str | None = None) -> str:
        extra = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=root,
            env={**env, **extra},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    shas = []
    for date, message, files in commits:
        for rel, content in files.items():
            path = root / rel
            if content is None:
                git("rm", "-q", rel)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            git("add", rel)
        git("commit", "-q", "-m", message, date=date)
        shas.append(git("rev-parse", "HEAD"))
    return shas


QUESTIONS = [
    (
        "syn-dec-01",
        "decision",
        "Which table holds the widget queue, and why?",
        ["docs/adr/ADR-001-queue.md"],
    ),
    ("syn-code-01", "code-location", "Where is enqueue_widget defined?", ["src/widgets/queue.py"]),
    ("syn-hist-01", "history", "When was the legacy blinker dropped?", ["#7"]),
    (
        "syn-trk-01",
        "tracker",
        "Which issue tracks sprockets jamming when the queue is full?",
        ["#3"],
    ),
    ("syn-learn-01", "learned", "Why must the sprocket cache be warmed first?", ["CLAUDE.md"]),
    ("syn-none-01", "unanswerable", "What p99 latency target do widget requests have?", []),
]


def _write_suite(suite: Path, sha: str, questions: list[tuple[str, str, str, list[str]]]) -> Path:
    """A synthetic suite: questions, the real prompts, a manifest with its hash."""
    (suite / "prompts").mkdir(parents=True)
    for name in ("agent.md", "judge.md"):
        (suite / "prompts" / name).write_text(
            (SUITE_DIR / "prompts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    items = [
        {
            "id": qid,
            "category": category,
            "question": question,
            "gold": "No such thing exists." if category == "unanswerable" else f"gold {qid}",
            "gold_verbatim": True,
            "evidence": evidence,
            "corpus": ["all"],
        }
        for qid, category, question, evidence in questions
    ]
    (suite / "questions.json").write_text(
        json.dumps({"suite": "ctxbench-synthetic", "version": "0.0.1", "questions": items}),
        encoding="utf-8",
    )
    mix: dict[str, int] = {}
    for _, category, _, _ in questions:
        mix[category] = mix.get(category, 0) + 1
    manifest = {
        "suite": "ctxbench-synthetic",
        "version": "0.0.1",
        "snapshot": {"sha": sha, "cutoff": CUTOFF},
        "mix": mix,
        "content_hash": suite_hash(suite),
    }
    (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return suite


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    base = tmp_path_factory.mktemp("ctxbench")
    shas = _make_repo(base / "src-repo")
    github = FakeGitHub()
    manifest = cli_ctxbench.record(
        shas[-1],
        base / "bundle",
        subject=SUBJECT,
        client=github,
        source=str(base / "src-repo"),
        cutoff=CUTOFF,
    )
    return {
        "base": base,
        "shas": shas,
        "bundle": base / "bundle",
        "manifest": manifest,
        "github": github,
        "suite": _write_suite(base / "suite", shas[-1], QUESTIONS),
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_CTXBENCH_SUBJECT", SUBJECT)
    monkeypatch.setenv("FORGE_CTXBENCH_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-never-used")


def _sqlite() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@contextmanager
def _postgres_schema(url: str) -> Iterator[sessionmaker[Session]]:
    """A fresh private schema on the test Postgres (a fresh database, for the bench)."""
    schema = f"ctxbench_{uuid.uuid4().hex[:12]}"
    boot = create_engine(url)
    with boot.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    parsed = make_url(url)
    engine = create_engine(
        parsed.set(query={**parsed.query, "options": f"-c search_path={schema},public"})
    )
    try:
        Base.metadata.create_all(engine)
        yield create_session_factory(engine)
    finally:
        engine.dispose()
        with boot.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        boot.dispose()


# --------------------------------------------------------------------------- #
# record / verify-bundle                                                       #
# --------------------------------------------------------------------------- #


def test_record_writes_a_raw_dump_and_a_pinned_bundle(corpus: dict[str, Any]) -> None:
    manifest = corpus["manifest"]
    assert manifest["sha"] == corpus["shas"][-1]
    assert manifest["subject_name"] == "widgets"  # never the owner
    assert manifest["cutoff"] == CUTOFF
    assert manifest["counts"] == {
        "github/issues.jsonl": 6,  # raw and unfiltered: the cut-off applies at build
        "github/pulls.jsonl": 3,
        "github/issue_comments.jsonl": 2,
        "github/review_comments.jsonl": 2,
    }
    assert sorted(manifest["files"]) == sorted(["repo.bundle", *manifest["counts"]])
    assert {(repo, path) for repo, path, _ in corpus["github"].calls} == {
        (SUBJECT, path) for path in LISTINGS
    }
    assert "acme-example" not in (corpus["bundle"] / "manifest.json").read_text(encoding="utf-8")
    assert cli_ctxbench.verify_bundle(corpus["bundle"]) == []


def test_record_cli_needs_the_token_and_refuses_a_used_directory(
    corpus: dict[str, Any], env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    argv = ["ctxbench", "record", "--sha", corpus["shas"][-1], "--out", str(tmp_path / "b")]
    argv += ["--cutoff", CUTOFF]
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.main(argv) == 2
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-fake-token")
    monkeypatch.setattr(cli_ctxbench, "_github_client", lambda token: FakeGitHub())
    assert cli.main([*argv, "--source", str(corpus["base"] / "src-repo")]) == 0
    assert cli.main(["ctxbench", "verify-bundle", str(tmp_path / "b")]) == 0
    assert cli.main([*argv, "--source", str(corpus["base"] / "src-repo")]) == 2  # not empty
    assert cli.main([*argv[:3], "abc123", *argv[4:]]) == 2  # not a full sha
    assert "ghp-fake-token" not in capsys.readouterr().out


def test_verify_bundle_catches_tampering(corpus: dict[str, Any], tmp_path: Path) -> None:
    copy = tmp_path / "bundle"
    subprocess.run(["cp", "-R", str(corpus["bundle"]), str(copy)], check=True)
    with (copy / "github" / "issues.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"number": 99}\n')
    assert cli_ctxbench.verify_bundle(copy) == [
        "github/issues.jsonl: sha256 mismatch",
        "github/issues.jsonl: 6 lines expected",
    ]
    assert cli.main(["ctxbench", "verify-bundle", str(copy)]) == 1


# --------------------------------------------------------------------------- #
# build / proxy over the committed corpus                                      #
# --------------------------------------------------------------------------- #

BASELINE = SUITE_DIR / "proxy-baseline.json"


def _proxy(factory: sessionmaker[Session], out: Path, monkeypatch: Any, *extra: str) -> int:
    monkeypatch.setattr(cli_ctxbench, "_session_factory", lambda: factory)
    return cli.main(["ctxbench", "proxy", "--out", str(out), *extra])


def _gate(result: Path, baseline: Path = BASELINE) -> int:
    return cli.main(["ctxbench", "gate", str(result), "--baseline", str(baseline)])


def test_proxy_over_the_committed_corpus_holds_the_committed_baseline(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _proxy(_sqlite(), tmp_path / "a.json", monkeypatch) == 0
    assert _proxy(_sqlite(), tmp_path / "b.json", monkeypatch) == 0
    first = (tmp_path / "a.json").read_bytes()
    assert first == (tmp_path / "b.json").read_bytes()  # byte-identical on fresh databases
    result, baseline = json.loads(first), json.loads(BASELINE.read_text(encoding="utf-8"))
    assert (result["lane"], result["embedder"]) == ("proxy", "none")
    assert result["content_hash"] == baseline["content_hash"] == suite_hash(SUITE_DIR)
    assert result["corpus_hash"] == baseline["corpus_hash"]
    assert result["tokens_p50"] <= 17_800  # spec J's ceiling; the 8,000 budget keeps it far off
    assert 0 < result["recall"] < 1  # a perfect proxy score would be a red flag
    assert _gate(tmp_path / "a.json") == 0


def test_a_lower_quality_assembly_fails_the_gate(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = cli_ctxbench.assemble

    def worse(factory: Any, workspace_id: uuid.UUID) -> Callable[[str], Mapping[str, Any]]:
        assemble = real(factory, workspace_id)

        def first_item_only(task: str) -> Mapping[str, Any]:
            bundle = assemble(task)
            return {**bundle, "items": bundle["items"][:1]}

        return first_item_only

    monkeypatch.setattr(cli_ctxbench, "assemble", worse)
    assert _proxy(_sqlite(), tmp_path / "worse.json", monkeypatch) == 0
    assert _gate(tmp_path / "worse.json") == 1


def _no_legs(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    return {**bundle, "legs": []}


def _raises(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    raise RuntimeError("bm25 statistics missing")


@pytest.mark.parametrize("failure", [_no_legs, _raises], ids=["no-legs", "assembly-raises"])
def test_an_unavailable_retrieval_leg_is_an_error_and_no_score(
    failure: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    real = cli_ctxbench.assemble

    def broken(factory: Any, workspace_id: uuid.UUID) -> Callable[[str], Mapping[str, Any]]:
        assemble = real(factory, workspace_id)
        return lambda task: failure(assemble(task))

    monkeypatch.setattr(cli_ctxbench, "assemble", broken)
    assert _proxy(_sqlite(), tmp_path / "r.json", monkeypatch) == 2
    assert not (tmp_path / "r.json").exists()
    err = capsys.readouterr().err
    assert "scf-dec-01: " in err and ("retrieval legs []" in err or "assembly failed" in err)


@pytest.mark.postgres
def test_proxy_is_byte_identical_on_fresh_postgres_databases(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, postgres_url: str
) -> None:
    outputs = []
    for name in ("a", "b"):
        with _postgres_schema(postgres_url) as factory:
            assert _proxy(factory, tmp_path / f"{name}.json", monkeypatch) == 0
            outputs.append((tmp_path / f"{name}.json").read_bytes())
    assert outputs[0] == outputs[1]
    assert _gate(tmp_path / "a.json") == 0


def test_build_ingests_the_committed_corpus_and_mints_a_viewer_key(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from scf_api.auth import service as auth_service
    from scf_db.models import RetrievalChunk

    factory = _sqlite()
    monkeypatch.setattr(cli_ctxbench, "_session_factory", lambda: factory)
    monkeypatch.setattr(auth_service, "_default_service", None)
    monkeypatch.setattr("scf_api.routers.knowledge.get_configured_embedder", lambda: None)
    assert cli.main(["ctxbench", "build"]) == 2  # no embedder and no --bm25-only
    assert cli.main(["ctxbench", "build", "--bm25-only"]) == 0
    out = capsys.readouterr().out
    workspace_id = uuid.UUID(out.split("workspace: ")[1].split()[0])
    token = out.rsplit(": ", 1)[1].strip()
    key = auth_service.get_auth_service().api_keys.verify(token)
    assert key is not None
    assert (key.workspace_id, key.role.value) == (workspace_id, "viewer")
    with factory() as session:
        rows = session.query(RetrievalChunk.path, RetrievalChunk.content).all()
    by_path = dict(rows)
    expected = {"CLAUDE.md", "docs/adr/0009-invoice-on-departure.md", "issues/169", "pulls/44"}
    assert expected | {"src/quayline/bookings/allocate.py"} <= set(by_path)
    assert any(path.startswith("commits/54d11fd16") for path in by_path)
    assert by_path["pulls/44"].startswith("PR #44 [merged 2026-03-20, merge commit 54d11fd16")
    assert by_path["pulls/111"].startswith("PR #111 [closed 2026-06-25] Retry the SMS gateway")
    assert by_path["issues/169"].startswith("Issue #169 [open] Storm alert reached only 50 of")
    assert "> Waiting is fine: the lock is held for milliseconds" in by_path["pulls/44"]


def _copy_suite(
    root: Path,
    *,
    questions: Callable[[list[dict[str, Any]]], None],
    corpus: Callable[[list[dict[str, Any]]], None],
) -> Path:
    """The shipped suite with its questions and corpus changed and both hashes recomputed."""
    suite = root / "suite"
    shutil.copytree(SUITE_DIR, suite)
    data = json.loads((suite / "questions.json").read_text(encoding="utf-8"))
    questions(data["questions"])
    (suite / "questions.json").write_text(json.dumps(data), encoding="utf-8")
    path = suite / "corpus.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    corpus(records)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = suite_hash(suite)
    manifest["corpus_hash"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return suite


def test_build_fails_when_a_question_is_in_the_corpus(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    def leak(questions: list[dict[str, Any]]) -> None:
        questions[3]["question"] = "Long berths are SCARCE."  # a sentence of an ADR

    def quote(records: list[dict[str, Any]]) -> None:
        """A question wrapped across two lines of an issue body (each line quoted ``> ``)."""
        issue = next(r for r in records if r.get("number") == 17)
        issue["body"] += "\n\nAsked at standup: Who is on call for\nQuayline outside office hours?"

    suite = _copy_suite(tmp_path, questions=leak, corpus=quote)
    monkeypatch.setattr(cli_ctxbench, "_session_factory", _sqlite)
    assert cli.main(["ctxbench", "build", "--bm25-only", "--suite", str(suite)]) == 1
    err = capsys.readouterr().err
    assert "question text found in the corpus: scf-dec-04, scf-none-03\n" in err


def test_build_and_proxy_refuse_a_corpus_that_does_not_match_its_hash(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite)
    with (suite / "corpus.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "file", "path": "NOTES.md", "text": "Extra."}) + "\n")
    monkeypatch.setattr(cli_ctxbench, "_session_factory", _sqlite)
    assert cli.main(["ctxbench", "build", "--bm25-only", "--suite", str(suite)]) == 2
    assert _proxy(_sqlite(), tmp_path / "r.json", monkeypatch, "--suite", str(suite)) == 2
    assert "does not match the manifest's" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


# --------------------------------------------------------------------------- #
# run / grade / report / gate (fake claude)                                    #
# --------------------------------------------------------------------------- #


def _live(corpus: dict[str, Any], *extra: str, suite: Path | None = None) -> list[str]:
    suite = suite or corpus["suite"]
    return [*extra, "--suite", str(suite), "--claude-bin", FAKE_CLAUDE, "--backoff", "0"]


def _suite(root: Path, corpus: dict[str, Any], *questions: str) -> Path:
    """A suite of these questions (ids ``syn-x-01`` on); the fake reads its mode from the text."""
    items = [
        (f"syn-x-{n:02d}", "decision", text, ["README.md"]) for n, text in enumerate(questions, 1)
    ]
    return _write_suite(root / "suite", corpus["shas"][-1], items)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_once(corpus: dict[str, Any], suite: Path, out: Path, arm: str, *extra: str) -> int:
    """One ask per question on one arm, in bank order."""
    argv = ["ctxbench", "run", "--out", str(out), "--arms", arm, "--reps", "1"]
    argv += ["--mcp-url", "http://forge.invalid/api/v1/mcp", "--concurrency", "1", *extra]
    argv += ["--embedder", "test-embedder"]
    return cli.main(_live(corpus, *argv, suite=suite))


def test_run_grade_report_gate(corpus: dict[str, Any], env: None, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    argv = ["ctxbench", "run", "--out", str(run_dir), "--arms", "none,forge"]
    argv += ["--mcp-url", "http://forge.invalid/api/v1/mcp", "--concurrency", "4"]
    argv += ["--embedder", "test-embedder"]
    assert cli.main(_live(corpus, *argv)) == 0
    asks = [json.loads(line) for line in (run_dir / "asks.jsonl").read_text().splitlines()]
    assert len(asks) == 2 * 6 * 3
    forge = [a for a in asks if a["arm"] == "forge"]
    assert {(a["mcp_calls"], a["tokens"], a["answer"]) for a in forge} == {(1, 3620, "found it")}
    assert {a["answer"] for a in asks if a["arm"] == "none"} == {"I don't know"}
    assert all(a["invalid"] == [] and a["attempts"] == 1 for a in asks)
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["claude_version"] == "9.9.9 (Claude Code, fake)"
    assert meta["agent"] == {"model": "claude-sonnet-5-5", "effort": "medium"}
    assert meta["content_hash"] == suite_hash(corpus["suite"])
    assert (meta["embedder"], meta["corpus_hash"]) == ("test-embedder", None)
    # every ask ran in its own directory under a temporary root, never under --out
    assert sorted(p.name for p in run_dir.iterdir()) == ["asks.jsonl", "run.json", "transcripts"]

    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir))) == 0
    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir))) == 2  # graded already
    assert cli.main(_live(corpus, "ctxbench", "regrade", str(run_dir))) == 0
    graded = [json.loads(line) for line in (run_dir / "graded.jsonl").read_text().splitlines()]
    assert {g["verdict"] for g in graded if g["arm"] == "forge"} == {"correct"}
    assert {g["verdict"] for g in graded if g["arm"] == "none"} == {"dont_know"}
    assert all((g["repeat_verdict"] is not None) == (g["rep"] == 1) for g in graded)

    written = tmp_path / "RESULTS.md"
    report = [
        "ctxbench",
        "report",
        str(run_dir),
        "--write",
        str(written),
        "--suite",
        str(corpus["suite"]),
    ]
    assert cli.main(report) == 0
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["valid"] is True
    assert summary["arms"]["forge"]["score"] == 1.0
    assert summary["arms"]["none"]["score"] == round(1 / 6, 4)
    assert summary["arms"]["forge"]["judge_agreement"] == 1.0
    assert (summary["lane"], summary["embedder"]) == ("live", "test-embedder")
    assert summary["commit"] == meta["forge_sha"] + (" (dirty)" if meta["forge_dirty"] else "")
    assert written.read_text() == (run_dir / "report.md").read_text()
    assert "The score is a measurement: no pass bar is set yet (#249)." in written.read_text()
    assert cli.main(["ctxbench", "gate", str(run_dir), "--bar", "acceptance"]) == 0
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(summary), encoding="utf-8")
    gate = ["ctxbench", "gate", str(run_dir), "--baseline", str(baseline)]
    assert cli.main(gate) == 0
    baseline.write_text(json.dumps({**summary, "content_hash": "sha256:other"}), encoding="utf-8")
    assert cli.main(gate) == 2  # a baseline from another bank
    transcripts = [*run_dir.glob("transcripts/*.jsonl"), *run_dir.glob("judge/*.jsonl")]
    assert len(transcripts) == 36 + 36 + 12
    for transcript in transcripts:
        cwd = Path(_rows(transcript)[0]["fake"]["cwd"])
        assert cwd.name == "work" and not cwd.exists()
        assert not cwd.is_relative_to(run_dir.resolve())
    assert not (run_dir / "tmp").exists()
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8")
    for name in ("run.json", "summary.json", "report.md"):
        assert "acme-example" not in (run_dir / name).read_text(encoding="utf-8")


def test_an_unreachable_forge_makes_the_run_invalid(
    corpus: dict[str, Any], env: None, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    argv = ["ctxbench", "run", "--out", str(run_dir), "--arms", "forge", "--reps", "1"]
    argv += ["--mcp-url", "http://unreachable.invalid/api/v1/mcp", "--embedder", "test-embedder"]
    assert cli.main(_live(corpus, *argv)) == 2
    asks = [json.loads(line) for line in (run_dir / "asks.jsonl").read_text().splitlines()]
    assert {(a["attempts"], tuple(a["invalid"])) for a in asks} == {
        (2, ("mcp server forge failed",))
    }
    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir))) == 2
    assert not list((run_dir).glob("judge/*"))  # an invalid ask is never judged
    assert cli.main(["ctxbench", "report", str(run_dir), "--suite", str(corpus["suite"])]) == 0
    assert json.loads((run_dir / "summary.json").read_text())["valid"] is False
    assert cli.main(["ctxbench", "gate", str(run_dir)]) == 2


def test_run_refuses_without_its_inputs(
    corpus: dict[str, Any], env: None, tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    base = ["ctxbench", "run", "--out", str(tmp_path / "r")]
    assert cli.main(_live(corpus, *base, "--arms", "forge")) == 2  # no --mcp-url
    assert cli.main(_live(corpus, *base, "--arms", "nonsense")) == 2
    forge = [*base, "--arms", "forge", "--mcp-url", "http://forge.invalid/api/v1/mcp"]
    assert cli.main(_live(corpus, *forge)) == 2  # a vector leg and no --embedder
    assert "--embedder" in capsys.readouterr().err
    monkeypatch.delenv("FORGE_CTXBENCH_SUBJECT")
    assert cli.main(_live(corpus, *base, "--arms", "none")) == 2
    assert cli.main(["ctxbench", "gate", str(tmp_path)]) == 2  # no summary there
    monkeypatch.setenv("FORGE_CTXBENCH_SUBJECT", SUBJECT)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    capsys.readouterr()
    assert cli.main(_live(corpus, *base, "--arms", "none")) == 2
    assert cli.main(_live(corpus, "ctxbench", "grade", str(tmp_path))) == 2
    assert capsys.readouterr().err.count("set ANTHROPIC_API_KEY: the live lane calls a model") == 2
    assert not (tmp_path / "r").exists()


def test_a_forge_tool_error_makes_the_run_invalid(
    corpus: dict[str, Any], env: None, tmp_path: Path
) -> None:
    """SCF answering the handshake and then failing a call is not a wrong answer."""
    suite = _suite(tmp_path, corpus, "What is plain?", "TOOLERROR what broke?")
    run_dir = tmp_path / "run"
    assert _run_once(corpus, suite, run_dir, "forge") == 2
    assert [(a["attempts"], a["invalid"]) for a in _rows(run_dir / "asks.jsonl")] == [
        (1, []),
        (2, ["forge tool error: mcp__forge__assemble_context: Error: HTTP 502 Bad Gateway"]),
    ]
    assert _run_once(corpus, suite, tmp_path / "rerank", "forge", "--rerank", "on") == 2
    assert _rows(tmp_path / "rerank" / "asks.jsonl")[0]["invalid"] == ["rerank off != on"]


def test_a_guard_stop_is_scored_and_other_failures_are_invalid(
    corpus: dict[str, Any], env: None, tmp_path: Path
) -> None:
    stopped = _suite(tmp_path / "s", corpus, "STOP:error_max_budget_usd", "STOP:error_max_turns")
    run_dir = tmp_path / "stopped"
    assert _run_once(corpus, stopped, run_dir, "forge") == 0
    asks = _rows(run_dir / "asks.jsonl")
    assert [(a["guard_tripped"], a["attempts"], a["invalid"]) for a in asks] == [(True, 1, [])] * 2
    assert json.loads((run_dir / "run.json").read_text())["guard_tripped"] == 2
    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir), suite=stopped)) == 0
    assert {g["verdict"] for g in _rows(run_dir / "graded.jsonl")} == {"correct"}
    assert cli.main(["ctxbench", "report", str(run_dir), "--suite", str(stopped)]) == 0
    summary = json.loads((run_dir / "summary.json").read_text())
    assert (summary["valid"], summary["arms"]["forge"]["score"]) == (True, 1.0)
    assert cli.main(["ctxbench", "gate", str(run_dir)]) == 2  # one ask a question, not three

    failed = _suite(
        tmp_path / "f", corpus, "STOP:error_during_execution", "MALFORMED?", "STRAYTOOL?"
    )
    assert _run_once(corpus, failed, tmp_path / "failed", "none") == 2
    asks = _rows(tmp_path / "failed" / "asks.jsonl")
    assert [(a["guard_tripped"], a["attempts"], a["invalid"]) for a in asks] == [
        (False, 2, ["result error_during_execution"]),
        (
            False,
            2,
            ["parse warning: stream line 2: TypeError: assistant message is a str, not a dict"],
        ),
        (False, 2, ["unexpected tools ['Bash']"]),
    ]


class _Interrupt(BaseException):
    """Stands in for the operator's Ctrl-C."""


def test_an_ask_that_raises_is_an_invalid_row_and_finished_asks_are_kept(
    corpus: dict[str, Any], env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scf_eval.ctxbench import claude

    real = claude.run_claude

    def flaky(call: claude.ClaudeCall, **kwargs: Any) -> claude.ClaudeResult:
        if "BRAVO" in call.prompt:
            raise RuntimeError("boom")
        if "DELTA" in call.prompt:
            raise _Interrupt
        return real(call, **kwargs)

    monkeypatch.setattr(claude, "run_claude", flaky)
    suite = _suite(tmp_path / "a", corpus, "ALPHA?", "BRAVO?", "CHARLIE?")
    run_dir = tmp_path / "run"
    assert _run_once(corpus, suite, run_dir, "none") == 2
    asks = _rows(run_dir / "asks.jsonl")
    assert [a["invalid"] for a in asks] == [[], ["ask failed: RuntimeError: boom"], []]
    assert json.loads((run_dir / "run.json").read_text())["invalid_asks"] == 1
    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir), suite=suite)) == 2
    assert [g["verdict"] for g in _rows(run_dir / "graded.jsonl")] == [
        "dont_know",
        None,
        "dont_know",
    ]

    cut_short = _suite(tmp_path / "b", corpus, "ALPHA?", "CHARLIE?", "DELTA?")
    with pytest.raises(_Interrupt):
        _run_once(corpus, cut_short, tmp_path / "cut", "none")
    kept = _rows(tmp_path / "cut" / "asks.jsonl")
    assert [(a["question_id"], a["invalid"]) for a in kept] == [("syn-x-01", []), ("syn-x-02", [])]


def test_grade_never_turns_a_malformed_judge_reply_into_a_verdict(
    corpus: dict[str, Any], env: None, tmp_path: Path
) -> None:
    suite = _suite(tmp_path, corpus, "What is plain?", "BADJUDGE what is odd?")
    run_dir = tmp_path / "run"
    assert _run_once(corpus, suite, run_dir, "none") == 0
    assert cli.main(_live(corpus, "ctxbench", "grade", str(run_dir), suite=suite)) == 2
    graded = _rows(run_dir / "graded.jsonl")
    assert [(g["verdict"], g["invalid"]) for g in graded] == [
        ("dont_know", []),
        (None, ["judge: judge reply is not JSON: 'I think this answer is fine.'"]),
    ]
    assert cli.main(["ctxbench", "report", str(run_dir), "--suite", str(suite)]) == 0
    assert json.loads((run_dir / "summary.json").read_text())["valid"] is False
    assert cli.main(["ctxbench", "gate", str(run_dir)]) == 2


def test_gate_cli_maps_a_result_or_baseline_of_another_shape_to_invalid(tmp_path: Path) -> None:
    def gate(result: Any, baseline: Any) -> int:
        (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (tmp_path / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        argv = ["ctxbench", "gate", str(tmp_path / "result.json")]
        return cli.main([*argv, "--baseline", str(tmp_path / "baseline.json")])

    proxy = {"kind": "ctxbench-proxy", "content_hash": "h", "hits": {"a": True}, "tokens_p50": 10}
    assert gate(proxy, proxy) == 0
    assert gate(proxy, {"content_hash": "h"}) == 2  # KeyError: no hits
    assert gate(proxy, {**proxy, "tokens_p50": "ten"}) == 2  # TypeError
    assert gate([proxy], proxy) == 2  # not an object
