# M — Bring your own tracker

**Date:** 2026-11-07
**Status:** Approved. M1 (GitHub Issues adapter) and M2 (tracker-wins write-back) implementation-ready;
M3 (issue-driven dispatch) implementation-ready after G1d's dispatcher lands.
**Depends on:** G (dispatcher), existing PM sync engine (F18/F40)
**Blocks:** K (automation lines trigger on tracker events)
**Decisions:** ADR-0019 (bring your own tracker), ADR-0007, ADR-0008, ADR-0014

---

## 1. Problem

Teams already run Linear, Jira or GitHub Issues. Forge's sync engine mirrors their issues into
forge's board and back, so forge competes for the record instead of serving it. Plain GitHub Issues
(issues + labels) have no adapter, yet labels are how lore's automation lines start work.

## 2. M1 — GitHub Issues adapter

`packages/integration-sdk/forge_integrations/pm/github_issues/` beside `github_projects/`, behind the
same `BaseAdapter` / `PMAdapter` contract, with `PMProvider.github_issues`:

- Status: open/closed plus a configurable label-to-status map (e.g. `status:in-progress`).
- Priority from `priority:*` labels (configurable map).
- Create, update, comment, and close/reopen through the existing `GitHubClient` (REST), with the
  installation-token path and its retry/redaction behaviour.
- Webhooks: `issues` (opened, edited, labeled, unlabeled, closed, reopened) and `issue_comment`,
  HMAC-verified like the existing GitHub webhook path; label events surface as `WebhookEvent`s.
- Pull requests are not issues: items with a `pull_request` key are skipped.
- Conformance: passes `packages/integration-sdk/tests/test_conformance.py` like every adapter.

## 3. M2 — Tracker wins

- `ConflictPolicy` default for a connected tracker becomes "external wins" for fields the tracker
  owns (title, description, status, assignee, priority, labels).
- Forge writes back only what it owns: a run-state comment (started, PR opened, failed), the PR link,
  and an optional mapped status transition the workspace configures. Writes carry the existing origin
  tag so the echo suppressor ignores them.
- The native board shows linked items with forge's data (specs, runs, approvals, cost) beside the
  tracker's fields; for unlinked teams it remains the record.

## 4. M3 — Issue-driven dispatch

- The dispatcher accepts `provider:key` (e.g. `linear:ENG-123`, `github_issues:acme/widgets#42`),
  resolves it through the PM link store to a work-item index row, and builds the objective from the
  tracker item (title, body, acceptance criteria parsed from a `## Acceptance criteria` section,
  repository from the connection's configured repo).
- Triggers: a configured label (default `forge:run`) or status transition, arriving by webhook,
  enqueues a run through the same path as `POST /agent/runs` (server-owned context, connected-repo
  check). Idempotent per `(issue, label event id)`.
- The run's outcome is written back per M2.
- Next: status-transition triggers, and label triggers for Linear/Jira (M3 ships GitHub Issues
  `issues.labeled` only).

## 5. Acceptance criteria

1. The GitHub Issues adapter passes the shared conformance suite and maps labels to status and
   priority both ways (tests against recorded GitHub API payloads).
2. An `issues.labeled` webhook with `forge:run` on a connected repository enqueues exactly one run;
   a replayed delivery enqueues none.
3. When the tracker and forge disagree on a tracker-owned field, the tracker's value wins and no
   write-back to the tracker occurs for that field.
4. A finished run posts one comment with its state and PR link to the originating issue.
5. A workspace with no tracker keeps today's native-board behaviour unchanged.

## 6. Out of scope

New tracker providers beyond GitHub Issues; PM OAuth app registration flows (tracked separately,
currently parked); two-way sync of comments authored by people.
