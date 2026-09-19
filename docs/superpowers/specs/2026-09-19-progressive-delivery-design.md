# B — Progressive delivery

**Date:** 2026-09-19
**Status:** DRAFT — scope and interfaces only. Revise from A's shadow-mode learnings before planning.
**Depends on:** A (`GateRequirements.deploy_mode`, changeset identity)
**Closes:** capability 7, mechanics half

---

## 1. Problem

`deploy-core` has a real FSM, a policy-driven gate, freeze windows and a rollback path. What it
lacks is any way to ship a change *partially*.

Verified absences: `feature.flag` — 3 incidental matches, no flag system. `canary` — 9 matches,
all in the replay/fork subsystem, none related to deployment.

The consequence is the model's batch problem: every change is all-or-nothing at promotion time,
so batch size drives both risk and delay, and the only lever left is to slow everything down.

## 2. Scope

Three capabilities, in dependency order.

### 2.1 Feature flags

A flag service with evaluation at the edge, flags bound to changeset identity from A, and
automatic staleness detection (a flag whose branches have not diverged in N days is reported as
dead). Flags are **not** a configuration store — no general-purpose key/value use.

### 2.2 Canary and ring deployment

Extend the `deploy-core` FSM with rollout strategies selected by A's `deploy_mode`:

| `deploy_mode` (from A) | Strategy |
|---|---|
| `auto` | direct promotion, current behaviour |
| `gated` | canary with health-gated promotion |

Canary promotion and abort are decided on **health signals**, not elapsed time. The Helm chart
already carries the primitives (Deployments, PDBs, ServiceMonitor); this adds the rollout
controller and the abort criteria, and does not replace the chart.

### 2.3 Rehearsed rollback

The model's diagnostic is *"how fast can you reverse a release — a rehearsed number, not a
documented procedure."* This adds a rollback rehearsal that runs on a schedule against a
non-production environment and records the measured reversal time as a metric.

## 3. Interfaces

- `packages/deploy-core/forge_deploy/rollout.py` — strategy selection and canary state
- `packages/deploy-core/forge_deploy/health_gate.py` — promotion and abort criteria from health
- New `flag` tables, workspace-scoped, changeset-linked
- `GET /api/deployments/{id}/rollout` — live rollout state
- `GET /api/deployments/rollback-drill` — last rehearsed reversal time

## 4. Acceptance criteria

1. A `critical`-class change deploys via canary without operator intervention.
2. A canary aborts automatically on a health-signal breach and the abort is recorded.
3. Rollback time is reported as a measured number from an actual rehearsal, with its timestamp.
4. Flags are evaluable at the edge and stale flags are reported.
5. A repo without A's `risk_rules:` keeps today's direct-promotion behaviour.

## 5. Open questions for after A

- Does canary belong in `deploy-core` or in a new `rollout-core`? Depends how much A's gate seam
  already carries.
- Flag evaluation in-process, or a sidecar? Depends on the latency budget the edge can accept.
- Do rings need separate modelling, or is ring N just a canary with a wider cohort?
