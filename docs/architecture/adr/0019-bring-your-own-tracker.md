# 0019. Bring your own tracker: work items live in the team's tracker

**Status:** Accepted
**Date:** 2026-11-07

## Context

Forge has a native board (tasks, epics, sprints) and a bidirectional sync engine
(`forge_integrations.pm.PMSyncEngine`) with adapters for Linear, Jira, GitHub Projects (draft
items), GitLab, Asana, ClickUp, Monday, Trello and a generic provider. The engine treats forge's
board as the hub: external issues are mirrored into forge tasks and back, so forge's board competes
with the tracker a team already uses. Plain GitHub Issues (with labels) have no adapter, although
lore's automation lines are driven by GitHub issue labels. Tervin's v3 plan adds a third board.
Teams rarely move their work items into a new tool to adopt a factory.

## Decision

- When a tracker is connected, **the tracker owns the work item** (title, description, status,
  assignee, comments). Forge keeps a work-item index: the external reference (`provider:key`) plus
  what forge alone owns (specs, runs, pull requests, approvals, cost, world facts, decisions).
- The default conflict policy is **tracker wins**; forge writes back only what it owns (run state,
  PR link, a summary comment, an optional status transition the team maps).
- Forge's board is a **view** over linked items and forge's data. The native board remains for teams
  without a tracker and for offline use.
- Add a **GitHub Issues** adapter (issues, labels, the `issues` webhook).
- The dispatcher accepts an external issue reference; tracker events (label added, status changed)
  can start runs and automation lines (spec K).

## Consequences

- Adoption without migration: a team keeps Linear, Jira or GitHub Issues.
- Forge, tervin and the tracker no longer compete for the record; tervin's board is also a view.
- Sync shrinks to write-back of forge-owned fields, which removes most conflict cases.
- PM OAuth and backfill (currently parked) become required for a smooth connection flow.
