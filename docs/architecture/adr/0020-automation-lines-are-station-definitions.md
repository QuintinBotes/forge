# 0020. Automation lines are station definitions run by a separate Temporal workflow and installed only from verified packages

**Status:** Accepted
**Date:** 2026-11-08

## Context

Spec K needs automation lines: workflow definitions with `agent`, `service` and `human` stations, distributed as signed `workflow_template` marketplace packages. Forge already has the parts to build on:
- the FSM DSL and a pure `TransitionEvaluator`;
- a hand-coded Temporal `FeatureWorkflow` for `default_feature`;
- the F28 definition and revision tables with run pinning;
- a marketplace in which `workflow_template` is a reserved kind with no installer.

Reading the code showed gaps that a line would hit:
- A package signature covers `manifest_hash`, but nothing ties the fetched manifest to that hash.
- The install service treats every kind that is not a skill as MCP.
- `workflow_run.current_state` is held to the 18 feature states by a CHECK constraint.
- The API's Temporal engine keeps its projection in memory.
- `ROLE_TOOLS` gives the implementer `open_pr`, which ADR-0014 forbids.

Lore's failures set the bar: auto-merge that fails open (#2374), "no checks" read as green (#2361), one failure freezing the backlog (#2370), and an unbounded weekend loop costing $2,415.

## Decision

- **DSL v2.** A line is a strict `LineDefinition` in `forge_contracts/lines.py`. Each non-terminal state carries one station. The station's outcome is the event, and the existing `TransitionEvaluator` routes it. Terminals are explicit. Outcomes with no edge park the run (for things a person can fix) or fail it (for failures). Validation rejects any route that turns a failure into success.
- **Behaviour lives in code.** Service behaviour comes only from a code-side catalog bound on the worker. An unbound service refuses (ADR-0007). Agent tools never include write channels. Agent outputs are never guards, and never feed inputs that decide where a finalizer writes. No merge-capable service exists.
- **People decide.** A human station resolves only through `ApprovalService`. The workflow reads the decision and the deciding user from Postgres. The Temporal Signal only wakes it and is never trusted. Station runs accept no free events. Only a WRITE user can resume or cancel one.
- **Separate runtime.** Lines run on a new `forge.StationWorkflow`. `FeatureWorkflow` and the V1 engine are unchanged, and V1 refuses lines with 503. `workflow_run.current_state` becomes an open string, and a new `workflow_station_visit` table records each station visit.
- **Verified installs.** `workflow_template` installs and updates only with a verified Ed25519 signature, at every trust level. For every kind, the manifest is now bound to the signed hash and to the index identity. Installed definitions are read-only and append-only, and an uninstall archives them. Runs keep their pinned revision.

## Consequences

- Forge can now run a line on Temporal and fail closed on any unconfigured or unverified dependency. The default feature path stays as it is.
- A compromised package, ticket or agent cannot merge, cannot push outside its run branch, and cannot approve.
- Deployments on `postgres_fsm` can install lines but cannot run them.
- Dropping the `current_state` CHECK moves state integrity into the application, and the migration's downgrade fails once line runs exist.
- Human stations are limited to the `spec`, `plan` and `pr` gates until ADR-0005's lockstep widening of the gate enum.
- Each new service needs a forge release. Authors who want to change an installed line must fork it, and the fork no longer receives package updates.
- Dependency-aware scheduling, tracker triggers and compensation on cancel are left for later.
- Before any line work, feature-run gates resolve through the approval service, and the transition
  route refuses the `*_by_human` events (spec K §4.7): the same path human stations use.
