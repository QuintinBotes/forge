# `release/evidence/` — cross-workstream gate evidence drop-zone

Some readiness gates are *owned* by other workstreams; they report `GREEN` only
once that workstream drops its evidence artifact here (or the file is stale/
absent, in which case the gate honestly reports `STALE` / `MISSING_EVIDENCE`):

| File | Gate | Owner | How to produce it |
|---|---|---|---|
| `coverage.json` | `G-COVERAGE` | HARD-12 (coverage) | `uv run pytest --cov --cov-report=json:release/evidence/coverage.json` (needs overall ≥ 93%). |
| `parked-closed.md` | `G-PARKED-CLOSED` | HARD-11 / HARD-14 | A dated sign-off that every parked V1 item is closed or has an owned, slice-linked deferral. |

These files are intentionally **not** committed as stubs — a stub would fake a
green gate. Their absence is the honest signal that the upstream evidence has not
landed yet.
