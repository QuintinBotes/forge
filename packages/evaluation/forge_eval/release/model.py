"""Typed core of the release-readiness engine: bars, gates, statuses, verdict.

Kept dependency-free (stdlib + PyYAML only, loaded lazily) so the meta-gate stays
robust across tool churn. The *bar-met* rule here is the structural honesty
guarantee of the whole slice.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Bar(enum.IntEnum):
    """Release bars, cumulative and ordered: ALPHA < BETA < PRODUCTION.

    A gate declared at a given bar is *selected* whenever the requested bar is
    at-or-above it (``gate.bar <= target``), which is what makes the bars
    cumulative (``production ⊇ beta ⊇ alpha``).
    """

    ALPHA = 1
    BETA = 2
    PRODUCTION = 3

    @classmethod
    def parse(cls, value: str | Bar) -> Bar:
        if isinstance(value, Bar):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:  # pragma: no cover - argparse guards the CLI
            valid = ", ".join(b.name.lower() for b in cls)
            raise ValueError(f"unknown bar {value!r}; expected one of {valid}") from exc

    @property
    def label(self) -> str:
        return self.name.capitalize()


class Status(enum.Enum):
    """Resolved state of a single gate.

    Only ``GREEN`` and ``MANUAL_ATTESTED`` count toward a bar being met; every
    other status (including ``SKIPPED_NO_CREDS`` and ``MANUAL_PENDING``) means the
    bar is NOT MET — the engine is honest, never optimistic.
    """

    GREEN = "GREEN"
    RED = "RED"
    SKIPPED_NO_CREDS = "SKIPPED_NO_CREDS"
    STALE = "STALE"
    MANUAL_PENDING = "MANUAL_PENDING"
    MANUAL_ATTESTED = "MANUAL_ATTESTED"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"

    @property
    def symbol(self) -> str:
        return {
            Status.GREEN: "🟢",
            Status.RED: "🔴",
            Status.SKIPPED_NO_CREDS: "⏭️",
            Status.STALE: "🟠",
            Status.MANUAL_PENDING: "🟡",
            Status.MANUAL_ATTESTED: "🟢",
            Status.MISSING_EVIDENCE: "⚪",
        }[self]


#: The only two statuses that satisfy a bar. Everything else ⇒ NOT MET.
MET_STATUSES: frozenset[Status] = frozenset({Status.GREEN, Status.MANUAL_ATTESTED})

#: Valid ``check.kind`` discriminators.
VALID_KINDS: frozenset[str] = frozenset({"command", "evidence", "manual"})


@dataclass(frozen=True)
class Gate:
    """One row of ``release/gates.yaml`` — a checkable release gate."""

    id: str
    bar: Bar
    workstream: str
    title: str
    check: dict[str, Any]
    blocker: int | None = None

    @property
    def kind(self) -> str:
        return str(self.check.get("kind", ""))

    @property
    def evidence_ref(self) -> str:
        """A short human reference to what this gate checks (cmd or artifact)."""
        kind = self.kind
        if kind == "command":
            return str(self.check.get("run", ""))
        if kind == "manual":
            return str(self.check.get("attestation", ""))
        if kind == "evidence":
            if "artifact" in self.check:
                return str(self.check["artifact"])
            all_of = self.check.get("all_of") or []
            return ", ".join(str(item.get("artifact", "")) for item in all_of)
        return ""


@dataclass
class GateResult:
    """The resolved outcome of evaluating one :class:`Gate`."""

    gate: Gate
    status: Status
    detail: str = ""
    checked_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def met(self) -> bool:
        return self.status in MET_STATUSES


def bar_met(results: Iterable[GateResult], target: Bar) -> bool:
    """True iff every gate at-or-below ``target`` is GREEN or MANUAL_ATTESTED.

    An empty selection is vacuously met (e.g. ``--bar alpha`` with no alpha
    gates), matching set semantics.
    """

    selected = [r for r in results if r.gate.bar <= target]
    return all(r.met for r in selected)


class ManifestError(ValueError):
    """Raised when ``release/gates.yaml`` is malformed or a gate is invalid."""


def _validate_check(gate_id: str, check: dict[str, Any]) -> None:
    kind = check.get("kind")
    if kind not in VALID_KINDS:
        raise ManifestError(
            f"gate {gate_id}: check.kind must be one of {sorted(VALID_KINDS)}, got {kind!r}"
        )
    if kind == "command" and not check.get("run"):
        raise ManifestError(f"gate {gate_id}: command check requires a non-empty 'run'")
    if kind == "manual" and not check.get("attestation"):
        raise ManifestError(f"gate {gate_id}: manual check requires an 'attestation' path")
    if kind == "evidence" and not (check.get("artifact") or check.get("all_of")):
        raise ManifestError(f"gate {gate_id}: evidence check requires 'artifact' or 'all_of'")


def parse_gates(raw: dict[str, Any]) -> list[Gate]:
    """Validate a parsed manifest mapping into typed :class:`Gate` objects."""

    if not isinstance(raw, dict) or "gates" not in raw:
        raise ManifestError("manifest must be a mapping with a top-level 'gates' list")
    entries = raw["gates"]
    if not isinstance(entries, list) or not entries:
        raise ManifestError("'gates' must be a non-empty list")

    gates: list[Gate] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestError(f"gate entry is not a mapping: {entry!r}")
        gate_id = entry.get("id")
        if not gate_id:
            raise ManifestError(f"gate entry missing 'id': {entry!r}")
        if gate_id in seen:
            raise ManifestError(f"duplicate gate id {gate_id!r}")
        seen.add(gate_id)
        try:
            bar = Bar.parse(entry["bar"])
        except (KeyError, ValueError) as exc:
            raise ManifestError(f"gate {gate_id}: {exc}") from exc
        check = entry.get("check")
        if not isinstance(check, dict):
            raise ManifestError(f"gate {gate_id}: 'check' must be a mapping")
        _validate_check(gate_id, check)
        blocker = entry.get("blocker")
        if blocker is not None and not isinstance(blocker, int):
            raise ManifestError(f"gate {gate_id}: 'blocker' must be an int or null")
        gates.append(
            Gate(
                id=str(gate_id),
                bar=bar,
                workstream=str(entry.get("workstream", "")),
                title=str(entry.get("title", "")),
                check=check,
                blocker=blocker,
            )
        )
    return gates


def load_gates(manifest_path: str | Path) -> list[Gate]:
    """Load + validate ``release/gates.yaml`` into typed gates."""

    import yaml  # lazy: mirrors forge_eval.golden's optional-YAML pattern

    path = Path(manifest_path)
    if not path.is_file():
        raise ManifestError(f"gate manifest not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_gates(raw)


def gates_for_bar(gates: Sequence[Gate], target: Bar) -> list[Gate]:
    """Gates selected by a target bar (at-or-below), preserving manifest order."""

    return [g for g in gates if g.bar <= target]
