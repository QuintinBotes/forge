"""``scf bench`` CLI (F35 §3.2) — offline benchmark authoring & verification.

The OSS-facing paths are offline + file-based, mirroring ``scf marketplace``:

* ``scf bench freeze <suite-dir>`` — compute the canonical ``content_hash``
  over the ordered cases + scoring, write it into ``manifest.yaml``, and mark
  the suite frozen. Exits ``1`` on post-freeze content drift (AC21) and when an
  ``agent_task`` case declares ``expected_terminal_state: merged`` (AC24).
* ``scf bench hash <suite-dir>`` — print the recomputed content hash (drift
  check; exit ``1`` when a frozen manifest no longer matches).
* ``scf bench verify --suite-dir <dir> --submission <file.json>`` — the
  reproduce path shipped on every public submission detail: deterministically
  replay the downloaded bundles against the frozen suite and exit ``0``
  (verified) / ``1`` (rejected), printing claimed vs reproduced (AC20/AC21).
* ``scf bench run --suite-dir <dir> --solver reference|empty [--out <dir>]`` — run
  a solver over a forge-factory-bench suite offline, in the local worktree
  sandbox, and print the Markdown report (#197). ``--out`` also writes
  ``report.json``/``report.md`` and keeps ``results.jsonl`` so a rerun resumes.
  Exits ``1`` when any case is an infrastructure error.

The DB/API-backed subcommands (``submit``/``leaderboard``) require a running SCF
API and are PARKED here (same precedent as the marketplace CLI); use
``/api/v1/benchmarks/*`` or the web UI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from scf_agent.sandbox import LocalSandboxProvider
from scf_eval.benchmark import (
    BenchmarkContentHashMismatch,
    BenchmarkError,
    BenchmarkScore,
    ReplayBundle,
    compute_content_hash,
    freeze,
    load_manifest,
    replay_bundles,
    verify_submission,
)
from scf_eval.sweval import CaseOutcome, empty_solver, reference_solver, run_suite

DEFAULT_EPSILON = 0.005


def _cmd_freeze(args: argparse.Namespace) -> int:
    suite_dir = Path(args.suite_dir)
    try:
        import yaml

        manifest, cases = load_manifest(suite_dir)
        frozen = freeze(manifest, cases)
    except (BenchmarkError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    manifest_path = suite_dir / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(frozen.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    print(f"frozen {frozen.slug}@{frozen.version} ({len(cases)} case(s))")
    print(f"content_hash: {frozen.content_hash}")
    return 0


def _cmd_hash(args: argparse.Namespace) -> int:
    try:
        manifest, cases = load_manifest(Path(args.suite_dir))
    except (BenchmarkError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(compute_content_hash(cases, manifest.scoring))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        manifest, cases = load_manifest(Path(args.suite_dir))
    except (BenchmarkContentHashMismatch, BenchmarkError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        raw = json.loads(Path(args.submission).read_text(encoding="utf-8"))
        claimed = BenchmarkScore.model_validate(raw["claimed"])
        bundles = [ReplayBundle.model_validate(b) for b in raw["bundles"]]
        claimed_hashes = [str(h) for h in raw.get("claimed_bundle_hashes", [])] or [
            b.content_hash for b in bundles
        ]
    except (KeyError, ValueError, OSError) as exc:
        print(f"error: invalid submission file: {exc}", file=sys.stderr)
        return 1

    report = replay_bundles(bundles, cases, manifest.scoring)
    result = verify_submission(
        claimed=claimed,
        reproduced_report=report,
        reproduced_bundles=bundles,
        claimed_bundle_hashes=claimed_hashes,
        scoring=manifest.scoring,
        cases=cases,
        epsilon=args.epsilon,
    )
    print(f"claimed:    {result.claimed_composite:.6f}")
    print(f"reproduced: {result.reproduced_composite:.6f}")
    print(f"delta:      {result.score_delta:.6f} (epsilon {result.epsilon})")
    print(f"bundle hashes match: {result.bundle_hash_matches}")
    for reason in result.reasons:
        print(f"reason: {reason}", file=sys.stderr)
    print("VERIFIED" if result.verified else "REJECTED")
    return 0 if result.verified else 1


def _git_head() -> str | None:
    """The commit of this checkout, for the report; None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _cmd_run(args: argparse.Namespace) -> int:
    suite_dir = Path(args.suite_dir)
    out = Path(args.out) if args.out else None
    try:
        solver = reference_solver(suite_dir) if args.solver == "reference" else empty_solver()
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
        report = asyncio.run(
            run_suite(
                suite_dir,
                solver,
                sandbox_provider=LocalSandboxProvider(),
                results_path=out / "results.jsonl" if out else None,
                scf_commit=_git_head(),
            )
        )
    except (BenchmarkError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    markdown = report.to_markdown()
    if out is not None:
        (out / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    infra = any(c.outcome == CaseOutcome.INFRASTRUCTURE_ERROR for c in report.cases)
    return 1 if infra else 0


def _requires_api(args: argparse.Namespace) -> int:  # pragma: no cover - parked path
    print(
        f"'{args.command}' talks to a running SCF API (/api/v1/benchmarks) — "
        "PARKED in this build; use the HTTP API or web UI.",
        file=sys.stderr,
    )
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scf bench")
    sub = parser.add_subparsers(dest="command", required=True)

    frz = sub.add_parser("freeze", help="compute + write content_hash; mark suite frozen")
    frz.add_argument("suite_dir", help="benchmark version dir containing manifest.yaml")
    frz.set_defaults(func=_cmd_freeze)

    hsh = sub.add_parser("hash", help="print the recomputed suite content hash")
    hsh.add_argument("suite_dir")
    hsh.set_defaults(func=_cmd_hash)

    ver = sub.add_parser("verify", help="offline replay verification (exit 0/1)")
    ver.add_argument("--suite-dir", dest="suite_dir", required=True)
    ver.add_argument("--submission", required=True, help="submission JSON (claimed+bundles)")
    ver.add_argument("--epsilon", type=float, default=DEFAULT_EPSILON)
    ver.set_defaults(func=_cmd_verify)

    run = sub.add_parser("run", help="run a solver over a suite offline and report (exit 0/1)")
    run.add_argument("--suite-dir", dest="suite_dir", required=True)
    run.add_argument("--solver", choices=("reference", "empty"), default="reference")
    run.add_argument("--out", help="directory for report.json, report.md and results.jsonl")
    run.set_defaults(func=_cmd_run)

    for name in ("submit", "leaderboard"):
        p = sub.add_parser(name, help=f"{name} (requires a running API — parked)")
        p.set_defaults(func=_requires_api)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
