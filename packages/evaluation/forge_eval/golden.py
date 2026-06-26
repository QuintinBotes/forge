"""Golden case model + loader for the evaluation harness.

A *golden case* pairs a query with the set of ground-truth ids that a correct
retrieval (or task) pipeline must surface. The full golden sets are produced by
Task 1.4 (>=15 retrieval pairs) and Task 1.16 (>=30 task inputs); this module is
the shared, dependency-light scaffold they load through.

Supported file formats: JSON (stdlib) and YAML (lazy-imported; only required if a
``.yaml``/``.yml`` file is loaded). The on-disk shape is either a top-level list
of case objects or a mapping with a ``cases:`` key.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["GoldenCase", "load_golden_set", "parse_golden_cases"]


@dataclass
class GoldenCase:
    """A single golden evaluation case."""

    id: str
    query: str
    expected_ids: list[str]
    kind: str = "retrieval"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _coerce_case(raw: Any) -> GoldenCase:
    if not isinstance(raw, dict):
        raise ValueError(f"each golden case must be a mapping, got {type(raw).__name__}")
    missing = {"id", "query"} - raw.keys()
    if missing:
        raise ValueError(f"golden case missing required field(s): {sorted(missing)}")
    expected = list(raw.get("expected_ids") or [])
    case_id = str(raw["id"])
    if not expected:
        raise ValueError(f"golden case {case_id!r} has empty expected_ids")
    return GoldenCase(
        id=case_id,
        query=str(raw["query"]),
        expected_ids=[str(x) for x in expected],
        kind=str(raw.get("kind", "retrieval")),
        tags=[str(t) for t in raw.get("tags", [])],
        metadata=dict(raw.get("metadata", {})),
    )


def parse_golden_cases(payload: Any) -> list[GoldenCase]:
    """Validate an already-parsed payload (list or ``{cases: [...]}``)."""
    if isinstance(payload, dict):
        payload = payload.get("cases", [])
    if not isinstance(payload, Iterable) or isinstance(payload, str | bytes):
        raise ValueError("golden set must be a list of cases or a mapping with 'cases'")
    cases = [_coerce_case(item) for item in payload]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate golden case id: {case.id!r}")
        seen.add(case.id)
    return cases


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


def load_golden_set(path: str | Path) -> list[GoldenCase]:
    """Load and validate a golden set from a JSON or YAML file."""
    resolved = Path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"golden set not found: {resolved}")
    return parse_golden_cases(_load_raw(resolved))
