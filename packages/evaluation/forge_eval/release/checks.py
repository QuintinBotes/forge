"""Check runners for the release-readiness engine.

Three ``check.kind`` resolvers, each returning a :class:`~forge_eval.release.model.Status`
plus a short redacted detail string:

- ``command`` — run a shell command (bounded by a timeout); if any ``required_env``
  is unset the command is **not run** and the gate is ``SKIPPED_NO_CREDS`` (honest,
  never GREEN-by-omission).
- ``evidence`` — stat + freshness-check an artifact, then run a predicate
  (``exists`` / ``json_all`` / ``json_path_eq`` / ``regex`` / ``cyclonedx_components_min``
  / ``coverage_min``).
- ``manual`` — read a signed attestation YAML; ``signed_off: true`` with
  ``by`` + ``date`` + ``link`` ⇒ ``MANUAL_ATTESTED``, else ``MANUAL_PENDING``.

Captured command output is defensively redacted so a gate that incidentally echoes
a secret env value cannot leak into a committed ``RELEASE_READINESS.md``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_eval.release.model import Status

# Env var names whose *values* must never appear in the rendered report.
_SECRET_NAME_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PRIVATE|_KEY|APIKEY|API_KEY)", re.IGNORECASE)
_OUTPUT_TAIL_CHARS = 800


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Mask any secret-looking env value that appears verbatim in ``text``."""

    environ = os.environ if env is None else env
    redacted = text
    for name, value in environ.items():
        if value and len(value) >= 4 and _SECRET_NAME_RE.search(name):
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def run_command_check(
    check: dict[str, Any],
    *,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[Status, str]:
    """Resolve a ``command`` gate. Missing creds ⇒ SKIPPED_NO_CREDS (no run)."""

    environ = dict(os.environ if env is None else env)
    required = check.get("required_env") or []
    missing = [name for name in required if not environ.get(name)]
    if missing:
        return (
            Status.SKIPPED_NO_CREDS,
            f"required env not set: {', '.join(sorted(missing))} (command not run)",
        )

    run = str(check["run"])
    # Prefer a real argv (no shell) unless the command uses shell features.
    use_shell = bool(re.search(r"[|&;><]|\$\(|`", run))
    args: Any = run if use_shell else shlex.split(run)
    try:
        completed = subprocess.run(
            args,
            shell=use_shell,
            cwd=str(cwd) if cwd else None,
            env=environ,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return (Status.RED, f"timed out after {timeout_seconds}s")
    except FileNotFoundError as exc:
        return (Status.RED, f"command not found: {exc}")

    tail = redact(_tail((completed.stdout or "") + "\n" + (completed.stderr or "")), environ)
    if completed.returncode == 0:
        return (Status.GREEN, "exit 0")
    return (Status.RED, f"exit {completed.returncode}: {tail}")


# --------------------------------------------------------------------------- #
# Evidence predicates
# --------------------------------------------------------------------------- #
def _resolve_json_path(data: Any, path: str) -> list[Any]:
    """Walk a dotted path with ``*`` wildcards, collecting all matching leaves."""

    nodes: list[Any] = [data]
    for part in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if part == "*":
                if isinstance(node, dict):
                    nxt.extend(node.values())
                elif isinstance(node, list):
                    nxt.extend(node)
            elif isinstance(node, dict) and part in node:
                nxt.append(node[part])
            # a missing key simply contributes no leaves
        nodes = nxt
    return nodes


def _predicate_exists(_path: Path, _pred: dict[str, Any]) -> tuple[bool, str]:
    return (True, "artifact present")


def _predicate_regex(path: Path, pred: dict[str, Any]) -> tuple[bool, str]:
    pattern = str(pred["pattern"])
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(pattern, text):
        return (True, f"matched /{pattern}/")
    return (False, f"pattern /{pattern}/ not found")


def _predicate_json_all(path: Path, pred: dict[str, Any]) -> tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    leaves = _resolve_json_path(data, str(pred["path"]))
    if not leaves:
        return (False, f"no values at path {pred['path']!r}")
    regex = re.compile(str(pred["matches"]))
    bad = [leaf for leaf in leaves if not (isinstance(leaf, str) and regex.fullmatch(leaf))]
    if bad:
        return (False, f"{len(bad)}/{len(leaves)} values fail /{pred['matches']}/")
    return (True, f"all {len(leaves)} values match /{pred['matches']}/")


def _predicate_json_path_eq(path: Path, pred: dict[str, Any]) -> tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    leaves = _resolve_json_path(data, str(pred["path"]))
    expected = pred["equals"]
    if leaves and all(leaf == expected for leaf in leaves):
        return (True, f"{pred['path']} == {expected!r}")
    return (False, f"{pred['path']} != {expected!r} (got {leaves!r})")


def _predicate_cyclonedx_components_min(path: Path, pred: dict[str, Any]) -> tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    components = data.get("components") or []
    minimum = int(pred.get("min", 1))
    count = len(components)
    if count >= minimum:
        return (True, f"{count} CycloneDX components (>= {minimum})")
    return (False, f"only {count} CycloneDX components (< {minimum})")


def _predicate_coverage_min(path: Path, pred: dict[str, Any]) -> tuple[bool, str]:
    minimum = float(pred.get("min", 0))
    text = path.read_text(encoding="utf-8")
    percent: float | None = None
    if path.suffix == ".json":
        data = json.loads(text)
        totals = data.get("totals") or data.get("meta") or {}
        raw = totals.get("percent_covered")
        if raw is not None:
            percent = float(raw)
    if percent is None:
        # coverage.xml carries a 0..1 line-rate attribute on the root element.
        match = re.search(r'line-rate="([0-9.]+)"', text)
        if match:
            percent = float(match.group(1)) * 100.0
    if percent is None:
        return (False, "could not parse a coverage percentage from artifact")
    if percent >= minimum:
        return (True, f"coverage {percent:.1f}% (>= {minimum}%)")
    return (False, f"coverage {percent:.1f}% (< {minimum}%)")


_PREDICATES = {
    "exists": _predicate_exists,
    "regex": _predicate_regex,
    "json_all": _predicate_json_all,
    "json_path_eq": _predicate_json_path_eq,
    "cyclonedx_components_min": _predicate_cyclonedx_components_min,
    "coverage_min": _predicate_coverage_min,
}


def _check_one_artifact(
    artifact: str,
    predicate: dict[str, Any],
    *,
    root: Path,
    max_age_days: int | None,
    now: float,
) -> tuple[Status, str]:
    path = (root / artifact).resolve()
    if not path.exists():
        return (Status.MISSING_EVIDENCE, f"artifact missing: {artifact}")
    if max_age_days is not None:
        age_days = (now - path.stat().st_mtime) / 86400.0
        if age_days > max_age_days:
            return (Status.STALE, f"artifact {artifact} is {age_days:.0f}d old (> {max_age_days}d)")
    ptype = str(predicate.get("type", "exists"))
    fn = _PREDICATES.get(ptype)
    if fn is None:
        return (Status.MISSING_EVIDENCE, f"unknown predicate type {ptype!r}")
    try:
        ok, detail = fn(path, predicate)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return (Status.MISSING_EVIDENCE, f"predicate {ptype} failed to read {artifact}: {exc}")
    return (Status.GREEN if ok else Status.MISSING_EVIDENCE, f"{artifact}: {detail}")


def run_evidence_check(
    check: dict[str, Any],
    *,
    root: Path,
    now: float | None = None,
) -> tuple[Status, str]:
    """Resolve an ``evidence`` gate (single ``artifact`` or ``all_of`` list)."""

    now = time.time() if now is None else now
    if "all_of" in check:
        details: list[str] = []
        for item in check["all_of"]:
            status, detail = _check_one_artifact(
                str(item["artifact"]),
                item.get("predicate") or {"type": "exists"},
                root=root,
                max_age_days=item.get("max_age_days"),
                now=now,
            )
            details.append(detail)
            if status is not Status.GREEN:
                return (status, "; ".join(details))
        return (Status.GREEN, "; ".join(details))

    return _check_one_artifact(
        str(check["artifact"]),
        check.get("predicate") or {"type": "exists"},
        root=root,
        max_age_days=check.get("max_age_days"),
        now=now,
    )


# --------------------------------------------------------------------------- #
# Manual attestation
# --------------------------------------------------------------------------- #
def _parse_iso_date(value: str) -> datetime | None:
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def run_manual_check(
    check: dict[str, Any],
    *,
    root: Path,
    now: float | None = None,
) -> tuple[Status, str]:
    """Resolve a ``manual`` gate. Never GREEN without a real signed attestation."""

    import yaml  # lazy

    rel = str(check["attestation"])
    path = (root / rel).resolve()
    if not path.exists():
        return (Status.MANUAL_PENDING, f"no attestation filed: {rel}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return (Status.MANUAL_PENDING, f"malformed attestation {rel}: {exc}")
    if not isinstance(data, dict):
        return (Status.MANUAL_PENDING, f"malformed attestation {rel}: not a mapping")

    if data.get("signed_off") is not True:
        return (Status.MANUAL_PENDING, "awaiting a signed human attestation")
    by = str(data.get("by") or "").strip()
    date = str(data.get("date") or "").strip()
    link = str(data.get("link") or "").strip()
    if not (by and date and link):
        return (
            Status.MANUAL_PENDING,
            "signed_off:true but missing one of by/date/link (incomplete attestation)",
        )

    max_age_days = check.get("max_age_days")
    if max_age_days is not None:
        signed = _parse_iso_date(date)
        if signed is None:
            return (Status.MANUAL_PENDING, f"unparseable attestation date {date!r}")
        now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz=UTC)
        age_days = (now_dt - signed).total_seconds() / 86400.0
        if age_days > max_age_days:
            return (Status.STALE, f"attestation {age_days:.0f}d old (> {max_age_days}d) — re-sign")

    return (Status.MANUAL_ATTESTED, f"signed off by {by} on {date}")
