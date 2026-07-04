"""``forge bench`` CLI (F35 §3.2) — offline benchmark authoring & verification.

The OSS-facing paths are offline + file-based, mirroring ``forge marketplace``:

* ``forge bench freeze <suite-dir>`` — compute the canonical ``content_hash``
  over the ordered cases + scoring, write it into ``manifest.yaml``, and mark
  the suite frozen. Exits ``1`` on post-freeze content drift (AC21) and when an
  ``agent_task`` case declares ``expected_terminal_state: merged`` (AC24).
* ``forge bench hash <suite-dir>`` — print the recomputed content hash (drift
  check; exit ``1`` when a frozen manifest no longer matches).
* ``forge bench verify --suite-dir <dir> --submission <file.json>`` — the
  reproduce path shipped on every public submission detail: deterministically
  replay the downloaded bundles against the frozen suite and exit ``0``
  (verified) / ``1`` (rejected), printing claimed vs reproduced (AC20/AC21).

The DB/API-backed subcommands (``run``/``submit``/``leaderboard``) require a
running Forge API and are PARKED here (same precedent as the marketplace CLI);
use ``/api/v1/benchmarks/*`` or the web UI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from forge_eval.benchmark import (
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


def _requires_api(args: argparse.Namespace) -> int:  # pragma: no cover - parked path
    print(
        f"'{args.command}' talks to a running Forge API (/api/v1/benchmarks) — "
        "PARKED in this build; use the HTTP API or web UI.",
        file=sys.stderr,
    )
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge bench")
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

    for name in ("run", "submit", "leaderboard"):
        p = sub.add_parser(name, help=f"{name} (requires a running API — parked)")
        p.set_defaults(func=_requires_api)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
