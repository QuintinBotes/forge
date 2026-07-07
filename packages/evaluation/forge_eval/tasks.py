"""Golden *task* set model + loader (Task 1.16).

Where :mod:`forge_eval.golden` models retrieval cases (query -> expected chunks),
this module models the broader **golden task set**: representative engineering
task inputs paired with their *known-good outputs* — the requirements a correct
run must satisfy, the terminal status it should reach, the verification checks it
must pass, and (optionally) the retrieval chunks it should surface.

Per spec (Observability and Evaluation) and the LangGraph 2026 production
guidance the spec cites, this golden set is built *before* framework complexity
is added and every release is scored against it so regressions block merge.

Supported file formats: JSON (stdlib) and YAML (lazy-imported). The on-disk shape
is either a top-level list of task objects or a mapping with a ``tasks:`` key.
Each requirement may be a bare id string (shorthand for a *core* requirement) or
a mapping ``{id, text?, difficulty?}`` where ``difficulty`` is ``core`` or
``stretch``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_contracts import TaskKind

__all__ = [
    "GoldenRequirement",
    "GoldenTask",
    "load_golden_tasks",
    "parse_golden_tasks",
]

#: Allowed requirement difficulty tiers. The reference baseline solver only
#: solves ``core`` requirements, so ``stretch`` requirements model harder goals
#: that differentiate pipelines.
_DIFFICULTIES = frozenset({"core", "stretch"})

#: Valid task kinds are exactly the frozen contract's :class:`TaskKind` values.
_VALID_KINDS = frozenset(k.value for k in TaskKind)


@dataclass(frozen=True)
class GoldenRequirement:
    """A single requirement a golden task must satisfy."""

    id: str
    text: str = ""
    difficulty: str = "core"


@dataclass
class GoldenTask:
    """A representative task input paired with its known-good output."""

    id: str
    objective: str
    kind: str = "feature"
    skill_profile: str | None = None
    requirements: list[GoldenRequirement] = field(default_factory=list)
    expected_status: str = "done"
    expected_checks: list[str] = field(default_factory=list)
    expected_chunks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def requirement_ids(self) -> list[str]:
        """All expected requirement ids, in declaration order."""
        return [r.id for r in self.requirements]

    @property
    def core_requirement_ids(self) -> list[str]:
        """Requirement ids excluding ``stretch`` goals (the baseline target)."""
        return [r.id for r in self.requirements if r.difficulty != "stretch"]


def _coerce_requirement(raw: Any, task_id: str) -> GoldenRequirement:
    if isinstance(raw, str):
        return GoldenRequirement(id=raw)
    if not isinstance(raw, dict):
        raise ValueError(
            f"task {task_id!r}: each requirement must be a string or mapping, "
            f"got {type(raw).__name__}"
        )
    if "id" not in raw:
        raise ValueError(f"task {task_id!r}: requirement missing required field 'id'")
    difficulty = str(raw.get("difficulty", "core"))
    if difficulty not in _DIFFICULTIES:
        raise ValueError(
            f"task {task_id!r}: requirement {raw['id']!r} has invalid difficulty "
            f"{difficulty!r} (expected one of {sorted(_DIFFICULTIES)})"
        )
    return GoldenRequirement(
        id=str(raw["id"]),
        text=str(raw.get("text", "")),
        difficulty=difficulty,
    )


def _coerce_task(raw: Any) -> GoldenTask:
    if not isinstance(raw, dict):
        raise ValueError(f"each golden task must be a mapping, got {type(raw).__name__}")
    missing = {"id", "objective"} - raw.keys()
    if missing:
        raise ValueError(f"golden task missing required field(s): {sorted(missing)}")
    task_id = str(raw["id"])

    kind = str(raw.get("kind", "feature"))
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"task {task_id!r}: unknown kind {kind!r} (expected one of {sorted(_VALID_KINDS)})"
        )

    requirements = [_coerce_requirement(r, task_id) for r in raw.get("requirements", [])]
    expected_chunks = [str(c) for c in raw.get("expected_chunks", [])]
    if not requirements and not expected_chunks:
        raise ValueError(
            f"task {task_id!r}: must declare at least one requirements entry or "
            f"expected_chunks entry to be gradeable"
        )

    return GoldenTask(
        id=task_id,
        objective=str(raw["objective"]),
        kind=kind,
        skill_profile=(str(raw["skill_profile"]) if raw.get("skill_profile") is not None else None),
        requirements=requirements,
        expected_status=str(raw.get("expected_status", "done")),
        expected_checks=[str(c) for c in raw.get("expected_checks", [])],
        expected_chunks=expected_chunks,
        tags=[str(t) for t in raw.get("tags", [])],
        metadata=dict(raw.get("metadata", {})),
    )


def parse_golden_tasks(payload: Any) -> list[GoldenTask]:
    """Validate an already-parsed payload (list or ``{tasks: [...]}``)."""
    if isinstance(payload, dict):
        payload = payload.get("tasks", [])
    if not isinstance(payload, Iterable) or isinstance(payload, str | bytes):
        raise ValueError("golden task set must be a list of tasks or a mapping with 'tasks'")
    tasks = [_coerce_task(item) for item in payload]
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ValueError(f"duplicate golden task id: {task.id!r}")
        seen.add(task.id)
    return tasks


def _load_raw(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # lazy: only needed for YAML golden sets
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without PyYAML
            raise RuntimeError(
                "PyYAML is required to load YAML golden sets; install it or use JSON"
            ) from exc
        return yaml.safe_load(text)
    return json.loads(text)


def load_golden_tasks(path: str | Path) -> list[GoldenTask]:
    """Load and validate a golden task set from a JSON or YAML file."""
    resolved = Path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"golden task set not found: {resolved}")
    return parse_golden_tasks(_load_raw(resolved))
