# D — Change economics

**Date:** 2026-09-19
**Status:** DRAFT — scope and interfaces only. Revise from A and C before planning.
**Depends on:** C (changeset identity plus outcome verdicts)
**Closes:** capability 10

---

## 1. Problem

Forge meters cost per **agent run** in tokens. It cannot say what a **delivered change** cost.

Verified absence: `cost_per|cost-per` — 0 files.

The model's diagnostic is *"what did the last change cost to deliver — a figure including review,
rework and support."* Token spend on one agent run is none of those things. Without this, the
factory can get efficient at producing things nobody can show were worth producing.

## 2. Scope

### 2.1 Cost per delivered change

Aggregate against C's changeset identity: agent-run token cost, CI compute, review time, rework
(subsequent changes to the same spec), and support/incident cost attributable to the change.

Review and rework are the parts that are usually omitted because they are inconvenient, and they
are usually the majority of the real figure.

### 2.2 Spend against delivered value

Pair cost with C's outcome verdicts. Not a claim to measure business value directly — a
juxtaposition of what a change cost against whether it worked.

### 2.3 Unpriced runs

F's local-CLI provider produces runs with real subscription cost and no per-token price. These are
marked `unpriced`, never zero. A zero would make local runs look free and corrupt every aggregate
here. The reporting must show unpriced volume alongside priced cost rather than silently blending
them.

## 3. Interfaces

- `packages/observability/forge_obs/cost/per_change.py` — aggregation over changeset identity
- `GET /api/cost/changes/{changeset_id}` — the full figure with its components itemised
- `GET /api/cost/flow` — cost against outcome across a window

## 4. Acceptance criteria

1. The last delivered change reports a cost figure with review and rework included and itemised.
2. Unpriced local-CLI volume is reported separately and never as zero cost.
3. Cost is reported per change and per system, never per person.
4. The figure reconciles with the existing per-run cost meter for the token component.

## 5. Open questions for after C

- Where does review time come from — PR timestamps, or explicit review events?
- How is rework attributed? Same spec key is the obvious join, but a follow-up fix under a new
  spec would be missed.
- Is support cost in scope at all for v1, or deferred? It is the hardest to attribute honestly.
