"""The release-readiness engine + ``forge-release-readiness`` CLI (HARD-12).

``forge-release-readiness --bar production --check`` runs or inspects every gate
in ``release/gates.yaml``, renders ``RELEASE_READINESS.md``, and returns a
CI-grade exit code (non-zero when the requested bar is NOT MET). It doubles as a
human report and a CI gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from forge_eval.release import checks
from forge_eval.release.model import (
    Bar,
    Gate,
    GateResult,
    Status,
    bar_met,
    load_gates,
)
from forge_eval.release.render import render_markdown

DEFAULT_MANIFEST = "release/gates.yaml"
DEFAULT_OUT = "RELEASE_READINESS.md"
DEFAULT_TIMEOUT_SECONDS = 1800


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_sha(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown" if out.returncode == 0 else "unknown"


def project_version(root: Path) -> str:
    """Read the single-source-of-truth version (root ``[project].version``)."""

    import tomllib

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return "unknown"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unknown"
    version = data.get("project", {}).get("version")
    return str(version) if version else "unknown"


def evaluate_gate(
    gate: Gate,
    *,
    root: Path,
    timeout_seconds: int,
    checked_at: str,
) -> GateResult:
    """Resolve a single gate to a :class:`GateResult`."""

    kind = gate.kind
    if kind == "command":
        status, detail = checks.run_command_check(
            gate.check, timeout_seconds=timeout_seconds, cwd=root
        )
    elif kind == "evidence":
        status, detail = checks.run_evidence_check(gate.check, root=root)
    elif kind == "manual":
        status, detail = checks.run_manual_check(gate.check, root=root)
    else:  # pragma: no cover - parse_gates rejects unknown kinds
        status, detail = Status.MISSING_EVIDENCE, f"unknown check kind {kind!r}"
    return GateResult(gate=gate, status=status, detail=detail, checked_at=checked_at)


def evaluate(
    gates: Sequence[Gate],
    *,
    root: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    checked_at: str | None = None,
) -> list[GateResult]:
    """Evaluate every gate in order, returning their results."""

    stamp = checked_at or _utc_now_iso()
    return [
        evaluate_gate(g, root=root, timeout_seconds=timeout_seconds, checked_at=stamp)
        for g in gates
    ]


def _select(gates: Sequence[Gate], target: Bar, only: set[str] | None) -> list[Gate]:
    selected = [g for g in gates if g.bar <= target]
    if only:
        selected = [g for g in selected if g.id in only]
    return selected


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge-release-readiness",
        description="Run/inspect every release gate and render RELEASE_READINESS.md.",
    )
    parser.add_argument(
        "--bar",
        choices=[b.name.lower() for b in Bar],
        default="beta",
        help="which bar must be MET (default: beta)",
    )
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="gate manifest path")
    parser.add_argument("--root", default=".", help="repo root for command/evidence resolution")
    parser.add_argument("--out", default=DEFAULT_OUT, help="rendered report path ('-' = stdout)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if the bar is NOT MET (CI mode)")
    mode.add_argument(
        "--report-only", action="store_true", help="always exit 0; just render (PR mode)"
    )
    parser.add_argument(
        "--only", default="", help="comma-separated gate ids to evaluate (debug subset)"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-command-gate timeout",
    )
    parser.add_argument(
        "--json", action="store_true", help="also emit machine-readable JSON to stderr"
    )
    return parser


def _result_json(results: Sequence[GateResult], target: Bar, met: bool) -> str:
    return json.dumps(
        {
            "bar": target.name.lower(),
            "met": met,
            "gates": [
                {
                    "id": r.gate.id,
                    "bar": r.gate.bar.name.lower(),
                    "status": r.status.value,
                    "met": r.met,
                    "detail": r.detail,
                }
                for r in results
            ],
        },
        indent=2,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    target = Bar.parse(args.bar)
    root = Path(args.root).resolve()
    only = {tok.strip() for tok in args.only.split(",") if tok.strip()} or None

    gates = load_gates(Path(args.manifest))
    selected = _select(gates, target, only)

    checked_at = _utc_now_iso()
    results = evaluate(
        selected, root=root, timeout_seconds=args.timeout_seconds, checked_at=checked_at
    )
    met = bar_met(results, target)

    report = render_markdown(
        results,
        target=target,
        git_sha=git_sha(root),
        cz_version=project_version(root),
        generated_at=checked_at,
    )

    if args.out == "-":
        sys.stdout.write(report)
    else:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}: {target.label} bar {'MET' if met else 'NOT MET'}")

    if args.json:
        sys.stderr.write(_result_json(results, target, met) + "\n")

    if args.report_only:
        return 0
    return 0 if met else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
