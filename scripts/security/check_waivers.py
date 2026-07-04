#!/usr/bin/env python3
"""Validate security/waivers.yaml (HARD-09) — fails closed.

Rules:
* every waiver carries id, tool, rule, location, finding, justification,
  owner, created, expires (no blank values);
* ids are unique;
* `expires` must be a date STRICTLY in the future — an expired waiver is a
  gate failure, not a silent ignore (accepted risk is re-reviewed, never
  permanent);
* `created` must not be in the future.

Exit code 0 = valid; 1 = any violation (printed one per line).
Used by scripts/security/run.sh and unit-tested with synthetic files in
tests/security/test_scanners_and_waivers.py.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import yaml

REQUIRED_FIELDS = (
    "id",
    "tool",
    "rule",
    "location",
    "finding",
    "justification",
    "owner",
    "created",
    "expires",
)


def _as_date(value: object, field: str, waiver_id: str, errors: list[str]) -> _dt.date | None:
    if isinstance(value, _dt.date):
        return value
    try:
        return _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        errors.append(f"{waiver_id}: field {field!r} is not an ISO date: {value!r}")
        return None


def validate_waivers(path: Path, *, today: _dt.date | None = None) -> list[str]:
    """Return a list of violations (empty = valid)."""
    today = today or _dt.date.today()
    errors: list[str] = []
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot read waivers file {path}: {exc}"]
    if not isinstance(doc, dict) or not isinstance(doc.get("waivers"), list):
        return [f"{path}: expected a top-level `waivers:` list"]

    seen: set[str] = set()
    for index, waiver in enumerate(doc["waivers"]):
        wid = str(waiver.get("id") or f"<waiver #{index}>")
        if not isinstance(waiver, dict):
            errors.append(f"{wid}: waiver entries must be mappings")
            continue
        for fields in REQUIRED_FIELDS:
            value = waiver.get(fields)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"{wid}: missing required field {fields!r}")
        if wid in seen:
            errors.append(f"{wid}: duplicate waiver id")
        seen.add(wid)

        created = _as_date(waiver.get("created"), "created", wid, errors)
        expires = _as_date(waiver.get("expires"), "expires", wid, errors)
        if created and created > today:
            errors.append(f"{wid}: created date {created} is in the future")
        if expires and expires <= today:
            errors.append(
                f"{wid}: waiver EXPIRED on {expires} — re-review the accepted "
                "risk or fix the finding (expired waivers fail closed)"
            )
    return errors


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("security/waivers.yaml")
    errors = validate_waivers(path)
    if errors:
        print(f"waiver validation FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"waivers OK: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
