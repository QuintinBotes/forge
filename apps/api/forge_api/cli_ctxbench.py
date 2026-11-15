"""``forge-cli ctxbench``: lore's agent-question benchmark on forge (spec J §2.4.1).

The wiring that needs the database, auth or GitHub; the logic lives in
:mod:`forge_eval.ctxbench`. Commands:

* ``record --sha S --out DIR``: the private corpus bundle. GitHub's issues,
  pulls, issue comments and review comments as raw, unfiltered JSON lines
  (``GitHubClient.iter_pages``), plus a ``git bundle`` of the pinned sha and a
  manifest of sha256s. The cut-off is recorded, and applied at build time.
* ``verify-bundle DIR``: sha256s, line counts, ``git bundle verify``, the sha.
* ``build --bundle DIR [--bm25-only]``: a fresh workspace, the repo files at
  the pinned sha and its commits (``git_log``), the tracker JSON through
  ``forge_knowledge.tracker`` as it stood at the cut-off (``at_cutoff`` plus
  the mappers' cutoff), an embedding-coverage check, the leakage check (no
  question text in the corpus), and a viewer key.
* ``proxy --bundle DIR --out FILE``: ``build --bm25-only``, then in-process
  ``assemble_context`` per question, then ``proxy-result.json``.
* ``run``, ``grade``, ``regrade``, ``report``, ``gate``: the live lane. Each
  ask and grade runs in its own directory under a temporary root, never under
  the run directory; ``asks.jsonl`` grows as the asks finish.

The subject (``<owner>/<repo>``) comes only from ``FORGE_CTXBENCH_SUBJECT``,
the GitHub token only from ``GITHUB_TOKEN`` and the benchmark key only from
``FORGE_CTXBENCH_TOKEN``; none is ever printed or written to a run record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from forge_eval.ctxbench.bank import (
    SUITE_DIR,
    Bank,
    BankError,
    Question,
    load_bank,
    subject_from_env,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_contracts.protocols import EmbeddingClient

__all__ = ["GROUP", "add_parsers", "run"]

GROUP = "ctxbench"
TOKEN_ENV = "FORGE_CTXBENCH_TOKEN"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

#: Raw listings recorded per bundle: file -> (``/repos/{repo}/<path>``, params).
_ITEMS = {"state": "all", "sort": "created", "direction": "asc", "per_page": "100"}
_COMMENTS = {"sort": "created", "direction": "asc", "per_page": "100"}
LISTINGS: dict[str, tuple[str, dict[str, str]]] = {
    "github/issues.jsonl": ("issues", _ITEMS),
    "github/pulls.jsonl": ("pulls", _ITEMS),
    "github/issue_comments.jsonl": ("issues/comments", _COMMENTS),
    "github/review_comments.jsonl": ("pulls/comments", _COMMENTS),
}
REPO_BUNDLE = "repo.bundle"
BUNDLE_REF = "refs/heads/ctxbench"

#: What the proxy asks ``assemble_context`` for (the MCP tool's defaults).
PROXY_BUDGET_TOKENS = 8_000
PROXY_TEMPLATE = "default"

ARMS = ("none", "forge")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_QUOTE_RE = re.compile(r"^> ?", re.MULTILINE)


class CtxbenchError(Exception):
    """A refusal with a message for the operator (exit 2)."""


class PageClient(Protocol):
    def iter_pages(
        self, repo: str, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# seams (tests replace these)                                                  #
# --------------------------------------------------------------------------- #


def _session_factory() -> sessionmaker[Session]:
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def _github_client(token: str) -> PageClient:
    from forge_integrations.github import GitHubClient

    return GitHubClient(token=token, timeout=30.0)


def _git(args: Sequence[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode:
        raise CtxbenchError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            count += 1
    return count


def _cutoff(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _bank(args: argparse.Namespace, env: Mapping[str, str]) -> Bank:
    return load_bank(Path(args.suite), subject=subject_from_env(env))


# --------------------------------------------------------------------------- #
# record / verify-bundle                                                       #
# --------------------------------------------------------------------------- #


def record(
    sha: str,
    out: Path,
    *,
    subject: str,
    client: PageClient,
    source: str,
    cutoff: str,
) -> dict[str, Any]:
    """Write the raw GitHub dump, the git bundle and the manifest; return the manifest."""
    if not _SHA_RE.fullmatch(sha):
        raise CtxbenchError("--sha must be a full 40-character commit sha")
    if out.exists() and any(out.iterdir()):
        raise CtxbenchError(f"{out} is not empty")
    (out / "github").mkdir(parents=True, exist_ok=True)
    counts = {
        name: _write_jsonl(out / name, client.iter_pages(subject, path, params))
        for name, (path, params) in LISTINGS.items()
    }
    with tempfile.TemporaryDirectory(prefix="ctxbench-record-") as tmp:
        mirror = Path(tmp) / "mirror.git"
        _git(["clone", "--bare", "--quiet", "--", source, str(mirror)])
        _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=mirror)
        _git(["update-ref", BUNDLE_REF, sha], cwd=mirror)
        _git(["bundle", "create", "-q", str((out / REPO_BUNDLE).resolve()), BUNDLE_REF], cwd=mirror)
    files = [REPO_BUNDLE, *LISTINGS]
    manifest = {
        "subject_name": subject.split("/", 1)[1],
        "sha": sha,
        "cutoff": cutoff,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": counts,
        "files": {name: _sha256(out / name) for name in files},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_bundle(bundle: Path) -> list[str]:
    """Problems with a recorded bundle (empty when it verifies)."""
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest.json: {exc}"]
    problems = []
    for name, digest in sorted(manifest.get("files", {}).items()):
        path = bundle / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif _sha256(path) != digest:
            problems.append(f"{name}: sha256 mismatch")
    for name, count in sorted(manifest.get("counts", {}).items()):
        if (bundle / name).is_file() and len(_jsonl(bundle / name)) != count:
            problems.append(f"{name}: {count} lines expected")
    if problems:
        return problems
    with tempfile.TemporaryDirectory(prefix="ctxbench-verify-") as tmp:
        try:
            _git(["init", "-q"], cwd=Path(tmp))
            _git(["bundle", "verify", "-q", str((bundle / REPO_BUNDLE).resolve())], cwd=Path(tmp))
            heads = _git(["bundle", "list-heads", str((bundle / REPO_BUNDLE).resolve())])
        except CtxbenchError as exc:
            return [str(exc)]
    if f"{manifest.get('sha')} {BUNDLE_REF}" not in heads.splitlines():
        problems.append(f"{REPO_BUNDLE}: does not pin {manifest.get('sha')}")
    return problems


# --------------------------------------------------------------------------- #
# build / proxy                                                                #
# --------------------------------------------------------------------------- #


def _by_number(items: Iterable[Mapping[str, Any]], url_key: str) -> dict[int, list[Any]]:
    grouped: dict[int, list[Any]] = defaultdict(list)
    for item in items:
        grouped[int(str(item[url_key]).rsplit("/", 1)[1])].append(item)
    return grouped


def tracker_chunks(bundle: Path, cutoff: datetime) -> list[Any]:
    """Issues and PRs as they stood at ``cutoff``, through ``forge_knowledge.tracker``."""
    from forge_knowledge.tracker import at_cutoff, issue_chunks, pull_chunks

    github = bundle / "github"
    comments = _by_number(_jsonl(github / "issue_comments.jsonl"), "issue_url")
    reviews = _by_number(_jsonl(github / "review_comments.jsonl"), "pull_request_url")
    chunks: list[Any] = []
    for issue in at_cutoff(_jsonl(github / "issues.jsonl"), cutoff):
        if "pull_request" not in issue:  # the issues listing includes PR stubs
            chunks += issue_chunks(issue, comments[issue["number"]], cutoff=cutoff)
    for pull in at_cutoff(_jsonl(github / "pulls.jsonl"), cutoff):
        number = pull["number"]
        chunks += pull_chunks(pull, comments[number], reviews[number], cutoff=cutoff)
    return chunks


def _normalise(text: str) -> str:
    """Case and spacing folded, without the ``> `` the tracker mappers put before a body line."""
    return " ".join(_QUOTE_RE.sub("", text).casefold().split())


def leaked_questions(
    factory: sessionmaker[Session], workspace_id: uuid.UUID, bank: Bank
) -> list[str]:
    """Ids of questions whose text appears verbatim (case and spacing aside) in the corpus."""
    from sqlalchemy import select

    from forge_db.models import RetrievalChunk

    pending = {q.id: _normalise(q.question) for q in bank.questions}
    leaked: list[str] = []
    with factory() as session:
        contents = session.scalars(
            select(RetrievalChunk.content)
            .where(RetrievalChunk.workspace_id == workspace_id)
            .execution_options(yield_per=500)
        )
        for content in contents:
            text = _normalise(content)
            for qid in [qid for qid, q in pending.items() if q in text]:
                leaked.append(qid)
                del pending[qid]
    return sorted(leaked)


def _unembedded(factory: sessionmaker[Session], workspace_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from forge_db.models import RetrievalChunk

    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(RetrievalChunk)
                .where(
                    RetrievalChunk.workspace_id == workspace_id,
                    RetrievalChunk.embedding.is_(None),
                )
            )
            or 0
        )


def ingest(
    factory: sessionmaker[Session],
    bundle: Path,
    bank: Bank,
    *,
    subject: str,
    embedder: EmbeddingClient | None,
) -> tuple[uuid.UUID, list[str]]:
    """Ingest a bundle into a fresh workspace; return its id and the failed checks."""
    from forge_db.models import KnowledgeSource, KnowledgeSourceKind, Workspace
    from forge_knowledge import KnowledgeService, full_sync, git_log, read_repo_files
    from forge_knowledge.tracker import commit_chunks

    problems = verify_bundle(bundle)
    if problems:
        raise CtxbenchError("bundle does not verify: " + "; ".join(problems))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    sha = manifest["sha"]
    if sha != bank.manifest["snapshot"]["sha"]:
        raise CtxbenchError(f"bundle pins {sha[:12]}, the bank was checked against another commit")
    cutoff = _cutoff(manifest["cutoff"])
    uri = f"https://github.com/{subject}"
    with factory() as session:
        slug = f"ctxbench-{manifest['subject_name']}-{sha[:8]}-{secrets.token_hex(3)}"
        workspace = Workspace(name=f"ctxbench {manifest['subject_name']} {sha[:8]}", slug=slug)
        session.add(workspace)
        session.flush()
        repo = KnowledgeSource(
            workspace_id=workspace.id, kind=KnowledgeSourceKind.REPO, name="repo", uri=uri
        )
        tracker = KnowledgeSource(
            workspace_id=workspace.id,
            kind=KnowledgeSourceKind.URL,
            name="tracker",
            uri=uri,
            config={"tracker": "github"},
        )
        session.add_all([repo, tracker])
        session.flush()
        workspace_id, repo_id, tracker_id = workspace.id, str(repo.id), str(tracker.id)
        session.commit()
    service = KnowledgeService.from_session_factory(factory, embedder, None)
    with tempfile.TemporaryDirectory(prefix="ctxbench-build-") as tmp:
        root = Path(tmp) / "repo"
        _git(
            [
                "clone",
                "--quiet",
                "--no-checkout",
                "--",
                str((bundle / REPO_BUNDLE).resolve()),
                str(root),
            ]
        )
        _git(["checkout", "--quiet", "--detach", sha], cwd=root)
        commits = [chunk for record in git_log(root, sha) for chunk in commit_chunks(record)]
        repo_result = full_sync(service, repo_id, read_repo_files(root), extra=commits)
    tracker_result = full_sync(service, tracker_id, {}, extra=tracker_chunks(bundle, cutoff))
    problems = [*repo_result.errors, *tracker_result.errors]
    if embedder is not None and (missing := _unembedded(factory, workspace_id)):
        problems.append(f"{missing} chunks have no embedding")
    leaked = leaked_questions(factory, workspace_id, bank)
    if leaked:
        problems.append(f"question text found in the corpus: {', '.join(leaked)}")
    return workspace_id, problems


def assemble(
    factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> Callable[[str], Mapping[str, Any]]:
    """In-process ``assemble_context`` over a BM25-only knowledge service."""
    from forge_api.services.context_service import assemble_context
    from forge_api.services.world_service import make_world_service
    from forge_knowledge import KnowledgeService

    knowledge = KnowledgeService.from_session_factory(factory, None, None)

    def _assemble(task: str) -> Mapping[str, Any]:
        with factory() as session:
            bundle = assemble_context(
                session=session,
                world=make_world_service(session),
                knowledge=knowledge,
                spec_engine=None,
                workspace_id=workspace_id,
                task=task,
                budget_tokens=PROXY_BUDGET_TOKENS,
                template=PROXY_TEMPLATE,
            )
            session.rollback()
        return bundle.model_dump(mode="json")

    return _assemble


def _cmd_build(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    bank = _bank(args, env)
    embedder = None
    if not args.bm25_only:
        from forge_api.routers.knowledge import get_configured_embedder

        embedder = get_configured_embedder()
        if embedder is None:
            raise CtxbenchError("no embedder configured (FORGE_EMBEDDING_*); or pass --bm25-only")
    factory = _session_factory()
    workspace_id, problems = ingest(
        factory, Path(args.bundle), bank, subject=subject_from_env(env), embedder=embedder
    )
    print(f"workspace: {workspace_id}")
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    from forge_api.auth.service import get_auth_service
    from forge_api.scripts.seed import warn_if_key_is_ephemeral
    from forge_contracts import UserRole

    warning = warn_if_key_is_ephemeral()
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    _info, token = get_auth_service().bootstrap_key(
        workspace_id=workspace_id, name="ctxbench-viewer", role=UserRole.VIEWER
    )
    print(f"viewer key (shown once; export it as {TOKEN_ENV}): {token}")
    return 0


def _cmd_proxy(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from forge_eval.ctxbench.proxy import proxy_json, run_proxy

    bank = _bank(args, env)
    factory = _session_factory()
    workspace_id, problems = ingest(
        factory, Path(args.bundle), bank, subject=subject_from_env(env), embedder=None
    )
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    result = run_proxy(bank, assemble(factory, workspace_id))
    Path(args.out).write_text(proxy_json(result, bank), encoding="utf-8")
    hits = [hit for hit in result.hits.values() if hit is not None]
    print(
        f"proxy: {sum(hits)}/{len(hits)} evidence hits, p50 bundle {result.tokens_p50:.0f} tokens"
    )
    return 0


# --------------------------------------------------------------------------- #
# run / grade / report / gate                                                  #
# --------------------------------------------------------------------------- #


def _forge_revision() -> tuple[str, bool]:
    here = Path(__file__).resolve().parent
    try:
        sha = _git(["rev-parse", "HEAD"], cwd=here).strip()
        dirty = bool(_git(["status", "--porcelain"], cwd=here).strip())
    except (CtxbenchError, OSError):
        return "", False
    return sha, dirty


def _ask(
    arm: str,
    question: Question,
    rep: int,
    *,
    args: argparse.Namespace,
    env: Mapping[str, str],
    subject: str,
    agent_prompt: str,
    run_dir: Path,
    scratch: Path,
) -> dict[str, Any]:
    from forge_eval.ctxbench.claude import (
        AGENT_EFFORT,
        AGENT_MODEL,
        ClaudeCall,
        ask_problems,
        mcp_config,
        run_claude,
    )
    from forge_eval.ctxbench.prompts import parse_agent_reply, render

    forge = arm == "forge"
    call = ClaudeCall(
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        prompt=render(agent_prompt, {"subject": subject, "question": question.question}),
        mcp_servers=mcp_config(args.mcp_url if forge else None, env.get(TOKEN_ENV)),
        timeout_s=args.timeout,
        claude_bin=args.claude_bin,
    )
    transcript = run_dir / "transcripts" / f"{arm}-{question.id}-{rep}.jsonl"
    legs = args.legs.split(",") if forge else None
    for attempt in (1, 2):  # one retry for an infrastructure failure
        result = run_claude(call, subject=subject, scratch=scratch, env=env, transcript=transcript)
        problems = ask_problems(
            result, expect_forge=forge, expected_legs=legs, expected_rerank=args.rerank
        )
        if not problems:
            break
        if attempt == 1:
            time.sleep(args.backoff)
    answer, sources = parse_agent_reply(result.text)
    return {
        "arm": arm,
        "question_id": question.id,
        "category": question.category,
        "rep": rep,
        "answer": answer,
        "sources": sources,
        "tokens": result.usage.total,
        "usage": {
            "input": result.usage.input,
            "output": result.usage.output,
            "cache_read": result.usage.cache_read,
            "cache_write": result.usage.cache_write,
        },
        "cost_usd": result.cost_usd,
        "duration_s": round(result.duration_s, 3),
        "turns": result.turns,
        "tool_calls": list(result.tool_calls),
        "mcp_calls": sum(name.startswith("mcp__") for name in result.tool_calls),
        "guard_tripped": result.guard_tripped,
        "attempts": attempt,
        "invalid": problems,
        "transcript": str(transcript.relative_to(run_dir)),
    }


#: What a row holds when its ask raised before it had a result.
_NO_RESULT: dict[str, Any] = {
    "answer": "",
    "sources": [],
    "tokens": 0,
    "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    "cost_usd": 0.0,
    "duration_s": 0.0,
    "turns": 0,
    "tool_calls": [],
    "mcp_calls": 0,
    "guard_tripped": False,
    "attempts": 1,
    "transcript": None,
}


def _ask_row(arm: str, question: Question, rep: int, **context: Any) -> dict[str, Any]:
    """One ask's row; an ask that raises is an invalid row, so it never ends the run."""
    try:
        return _ask(arm, question, rep, **context)
    except Exception as exc:  # whatever it was, the other asks still count
        return {
            **_NO_RESULT,
            "arm": arm,
            "question_id": question.id,
            "category": question.category,
            "rep": rep,
            "invalid": [f"ask failed: {type(exc).__name__}: {exc}"],
        }


def _cmd_run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from forge_eval.ctxbench.claude import AGENT_EFFORT, AGENT_MODEL, JUDGE_MODEL, claude_version
    from forge_eval.ctxbench.prompts import load_prompts, prompt_hashes

    arms = [arm for arm in args.arms.split(",") if arm]
    if not arms or set(arms) - set(ARMS):
        raise CtxbenchError(f"--arms must name some of {', '.join(ARMS)}")
    if "forge" in arms and not (args.mcp_url and env.get(TOKEN_ENV)):
        raise CtxbenchError(f"the forge arm needs --mcp-url and {TOKEN_ENV}")
    run_dir = Path(args.out)
    if run_dir.exists():
        raise CtxbenchError(f"{run_dir} exists; a run never overwrites another")
    bank = _bank(args, env)
    subject = subject_from_env(env)
    agent_prompt = load_prompts(Path(args.suite)).agent
    run_dir.mkdir(parents=True)
    sha, dirty = _forge_revision()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    jobs = [
        (arm, q, rep) for arm in arms for q in bank.questions for rep in range(1, args.reps + 1)
    ]
    rows = []
    with (
        tempfile.TemporaryDirectory(prefix="ctxbench-run-") as scratch,
        ThreadPoolExecutor(max_workers=args.concurrency) as pool,
        (run_dir / "asks.jsonl").open("w", encoding="utf-8") as handle,
    ):
        asked = pool.map(
            lambda job: _ask_row(
                *job,
                args=args,
                env=env,
                subject=subject,
                agent_prompt=agent_prompt,
                run_dir=run_dir,
                scratch=Path(scratch),
            ),
            jobs,
        )
        for row in asked:  # written as each finishes: a run that stops keeps its asks
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            rows.append(row)
    meta = {
        "suite": bank.suite,
        "version": bank.version,
        "content_hash": bank.content_hash,
        "prompts_sha256": prompt_hashes(Path(args.suite)),
        "forge_sha": sha,
        "forge_dirty": dirty,
        "claude_version": claude_version(args.claude_bin),
        "agent": {"model": AGENT_MODEL, "effort": AGENT_EFFORT},
        "judge": {"model": JUDGE_MODEL},
        "arms": arms,
        "reps": args.reps,
        "concurrency": args.concurrency,
        "legs": args.legs.split(","),
        "rerank": args.rerank,
        "timeout_s": args.timeout,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "asks": len(rows),
        "invalid_asks": sum(bool(row["invalid"]) for row in rows),
        "guard_tripped": sum(row["guard_tripped"] for row in rows),
        "cost_usd": round(sum(row["cost_usd"] for row in rows), 4),
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"run: {len(rows)} asks, {meta['invalid_asks']} invalid, ${meta['cost_usd']}")
    return 2 if meta["invalid_asks"] else 0


def _judged(
    row: Mapping[str, Any],
    *,
    question: Question,
    rubric: str,
    args: argparse.Namespace,
    env: Mapping[str, str],
    subject: str,
    run_dir: Path,
    scratch: Path,
) -> dict[str, Any]:
    from forge_eval.ctxbench.claude import ClaudeCall, ClaudeResult, run_claude
    from forge_eval.ctxbench.judge import JudgeError, Judgement, judge

    graded = {
        k: row[k]
        for k in ("arm", "question_id", "category", "rep", "tokens", "cost_usd", "duration_s")
    }
    graded.update(mcp_calls=row["mcp_calls"], invalid=list(row["invalid"]))
    graded.update(verdict=None, reason=None, repeat_verdict=None, judge_cost_usd=0.0)
    if graded["invalid"]:
        return graded
    name = f"{row['arm']}-{row['question_id']}-{row['rep']}"

    def grade_once(tag: str) -> tuple[str, str, float]:
        def runner(call: ClaudeCall) -> ClaudeResult:
            transcript = run_dir / "judge" / f"{name}{tag}.jsonl"
            return run_claude(
                call, subject=subject, scratch=scratch, env=env, transcript=transcript
            )

        def once() -> Judgement:
            return judge(
                question, row["answer"], rubric=rubric, runner=runner, claude_bin=args.claude_bin
            )

        try:
            j = once()
        except JudgeError:  # one retry, as for an ask
            time.sleep(args.backoff)
            j = once()
        return j.verdict, j.reason, j.cost_usd

    try:
        graded["verdict"], graded["reason"], cost = grade_once("")
        graded["judge_cost_usd"] = cost
        if row["rep"] == 1:  # the first ask is graded twice to measure the judge
            graded["repeat_verdict"], _, cost = grade_once("-repeat")
            graded["judge_cost_usd"] += cost
    except JudgeError as exc:
        graded["invalid"] = [f"judge: {exc}"]
    return graded


def _cmd_grade(args: argparse.Namespace, env: Mapping[str, str], *, regrade: bool) -> int:
    from forge_eval.ctxbench.prompts import load_prompts

    run_dir = Path(args.run_dir)
    target = run_dir / "graded.jsonl"
    if target.exists() and not regrade:
        raise CtxbenchError(f"{target} exists; use regrade to judge the stored answers again")
    bank = _bank(args, env)
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if not regrade and run_record["content_hash"] != bank.content_hash:
        raise CtxbenchError("the run was asked from another bank; use regrade with its suite")
    questions = bank.by_id()
    rubric = load_prompts(Path(args.suite)).judge
    asks = _jsonl(run_dir / "asks.jsonl")
    unknown = sorted({row["question_id"] for row in asks} - set(questions))
    if unknown:
        raise CtxbenchError(f"the bank has no question {unknown[0]}")
    subject = subject_from_env(env)
    with (
        tempfile.TemporaryDirectory(prefix="ctxbench-grade-") as scratch,
        ThreadPoolExecutor(max_workers=args.concurrency) as pool,
    ):
        rows = list(
            pool.map(
                lambda row: _judged(
                    row,
                    question=questions[row["question_id"]],
                    rubric=rubric,
                    args=args,
                    env=env,
                    subject=subject,
                    run_dir=run_dir,
                    scratch=Path(scratch),
                ),
                asks,
            )
        )
    _write_jsonl(target, rows)
    invalid = sum(bool(row["invalid"]) for row in rows)
    print(f"graded: {len(rows)} asks, {invalid} invalid")
    return 2 if invalid else 0


def _cmd_report(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from forge_eval.ctxbench.report import build_summary, render_markdown

    run_dir = Path(args.run_dir)
    bank = _bank(args, env)
    reference = json.loads((Path(args.suite) / "reference.json").read_text(encoding="utf-8"))
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary = build_summary(
        _jsonl(run_dir / "graded.jsonl"), bank=bank, reference=reference, run=run_record
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = render_markdown(summary)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    if args.write:
        Path(args.write).write_text(markdown, encoding="utf-8")
    print(f"report: {run_dir / 'summary.json'}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from forge_eval.ctxbench.gate import ACCEPTANCE, FLOOR, GateResult, gate_live, gate_proxy

    path = Path(args.path)
    target = path / "summary.json" if path.is_dir() else path
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        baseline = (
            json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
        )
        if data.get("kind") == "ctxbench-proxy":
            result = gate_proxy(data, baseline=baseline)
        elif data.get("kind") == "ctxbench-live":
            bar = ACCEPTANCE if args.bar == "acceptance" else FLOOR
            result = gate_live(data, bar=bar, baseline=baseline)
        else:
            result = GateResult("invalid", (f"{target} is not a ctxbench result",))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # unreadable, or a result or baseline of another shape: never a pass or a bar miss
        result = GateResult("invalid", (f"cannot read the result or baseline: {exc!r}",))
    print(f"gate: {result.status}")
    for reason in result.reasons:
        print(f"  {reason}")
    return result.exit_code


# --------------------------------------------------------------------------- #
# parser + dispatch                                                            #
# --------------------------------------------------------------------------- #


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``forge-cli ctxbench <command>``."""
    group = sub.add_parser(GROUP, help="lore's agent-question benchmark (spec J §2.4.1)")
    csub = group.add_subparsers(dest="command", required=True)

    def suite(p: argparse.ArgumentParser) -> None:
        p.add_argument("--suite", default=str(SUITE_DIR), help="suite directory (bank + prompts)")

    def live(p: argparse.ArgumentParser) -> None:
        p.add_argument("--claude-bin", default="claude")
        p.add_argument("--concurrency", type=int, default=3)
        p.add_argument("--backoff", type=float, default=10.0, help="seconds before the one retry")

    snapshot = json.loads((SUITE_DIR / "manifest.json").read_text(encoding="utf-8"))["snapshot"]
    rec = csub.add_parser("record", help="record the private corpus bundle (GitHub, once)")
    rec.add_argument("--sha", default=snapshot["sha"])
    rec.add_argument("--out", required=True)
    rec.add_argument("--cutoff", default=snapshot["cutoff"])
    rec.add_argument("--source", default=None, help="clone source (default: the GitHub repo)")
    ver = csub.add_parser("verify-bundle", help="check a bundle's sha256s, counts and git bundle")
    ver.add_argument("bundle")
    build = csub.add_parser("build", help="ingest a bundle into a fresh workspace + viewer key")
    build.add_argument("--bundle", required=True)
    build.add_argument("--bm25-only", action="store_true")
    suite(build)
    proxy = csub.add_parser("proxy", help="BM25-only build, then the evidence proxy")
    proxy.add_argument("--bundle", required=True)
    proxy.add_argument("--out", default="proxy-result.json")
    suite(proxy)
    run_p = csub.add_parser("run", help="ask every question (Anthropic + forge)")
    run_p.add_argument("--out", required=True, help="new run directory")
    run_p.add_argument("--arms", default="forge")
    run_p.add_argument("--reps", type=int, default=3)
    run_p.add_argument("--mcp-url", default=None)
    run_p.add_argument("--legs", default="bm25,vector", help="legs the forge arm must report")
    run_p.add_argument("--rerank", default=None, help="rerank status the forge arm must report")
    run_p.add_argument("--timeout", type=float, default=600.0)
    suite(run_p)
    live(run_p)
    for name in ("grade", "regrade"):
        grade = csub.add_parser(name, help=f"{name} a run's answers with the Opus judge")
        grade.add_argument("run_dir")
        suite(grade)
        live(grade)
    report = csub.add_parser("report", help="summary.json + markdown report")
    report.add_argument("run_dir")
    report.add_argument("--write", default=None, help="also write the markdown here")
    suite(report)
    gate = csub.add_parser("gate", help="exit 0 pass, 1 fail, 2 invalid")
    gate.add_argument("path", help="a run directory or proxy-result.json")
    gate.add_argument("--baseline", default=None)
    gate.add_argument("--bar", choices=("floor", "acceptance"), default="floor")


def _dispatch(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    command = args.command
    if command == "record":
        token = env.get(GITHUB_TOKEN_ENV, "")
        if not token:
            raise CtxbenchError(f"set {GITHUB_TOKEN_ENV}")
        subject = subject_from_env(env)
        manifest = record(
            args.sha,
            Path(args.out),
            subject=subject,
            client=_github_client(token),
            source=args.source or f"https://github.com/{subject}.git",
            cutoff=args.cutoff,
        )
        print(f"recorded {manifest['counts']} at {manifest['sha'][:12]}")
        return 0
    if command == "verify-bundle":
        problems = verify_bundle(Path(args.bundle))
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        print("bundle: " + ("invalid" if problems else "ok"))
        return 1 if problems else 0
    if command == "build":
        return _cmd_build(args, env)
    if command == "proxy":
        return _cmd_proxy(args, env)
    if command == "run":
        return _cmd_run(args, env)
    if command in ("grade", "regrade"):
        return _cmd_grade(args, env, regrade=command == "regrade")
    if command == "report":
        return _cmd_report(args, env)
    return _cmd_gate(args)


def run(args: argparse.Namespace, *, env: Mapping[str, str] | None = None) -> int:
    try:
        return _dispatch(args, os.environ if env is None else env)
    except (CtxbenchError, BankError) as exc:
        print(f"forge-cli ctxbench {args.command}: {exc}", file=sys.stderr)
        return 2
