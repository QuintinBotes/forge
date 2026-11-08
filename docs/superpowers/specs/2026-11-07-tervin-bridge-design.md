# L — Tervin bridge

**Date:** 2026-11-07
**Status:** Seams 1–3 implementation-ready (seam 2 built); seams 4–6 DRAFT, scope and interfaces only
**Depends on:** I (forge MCP server), G (real runs and their events)
**Blocks:** nothing in forge
**Decisions:** ADR-0002 (forge is the system of record, tervin its desktop face), ADR-0013 (one
run-event vocabulary), ADR-0015 (desktop integration contract)

---

## 1. The question this answers

*How can the factory eventually be used by, and incorporated into, tervin?*

Tervin (local v3 tree, 216 commits ahead of GitHub) now plans to own a board, specs, a coordinator
and approvals — the same things forge owns. Its own plan (#118) says it will call forge "only if
Tervin gets a team server". **Forge is that team server.** The conflict resolves by layer, not by
winner:

| Layer | Owner | Why |
|---|---|---|
| Board, specs, approvals (record), policy, world model, knowledge, audit, cost | **forge** | Multi-user, durable, governed; tervin is single-user with unstable local formats |
| Terminal, PTY, shell integration, macOS (Touch ID, notifications), local gated agent runs | **tervin** | Platform-bound; cannot run in a server or a browser |
| Approving | **both surfaces, one record** | Tervin answers forge approvals locally (Touch ID); forge records the decision and audits it |

No code moves in either direction. Forge is Apache-2.0 and tervin MIT; integration is by protocol
only, so the licence direction question never arises.

## 2. Seams, in the order to build them

Tervin admits a tool only as a process, a CLI whose JSON it draws, MCP, or `.tervin/` data.

| # | Seam | Forge side | Tervin side |
|---|---|---|---|
| 1 | **MCP** | Spec I. Adds `spec_get`, `spec_list`, `board_items`, `approvals_pending`, `run_status` tools through I's registry | One `mcp.json` entry; token from a named env var; no code change |
| 2 | **`forge --json`** | `forge-cli` subcommands `board`, `specs`, `approvals`, `runs` with `--json` (stable, versioned schema) | Draws them natively as a "CLI whose JSON tervin draws" |
| 3 | **Run event stream** | `GET /agent/runs/{id}/events` (SSE) emitting the run-event vocabulary with an `enforcement: gated|observed` flag on each event (ADR-0013) | Renders a forge run as a Thread |
| 4 | **Approval push** | Approval requested/resolved events on `/ws` for the principal's workspace | Inbox + Touch ID; posts the decision to `POST /approvals/{id}/decision` |
| 5 | **Runner protocol** | Register a runner, lease a task, stream events, submit result + attestation inputs | Runs the agent locally behind Tervin Rules; forge records it as `gated` with tervin as the gate |
| 6 | **ACP shim** | `forge acp` stdio wrapper around a forge run: forge tool calls map to `session/request_permission` | Treats forge as one more ACP agent it can gate |

### 2.1 Seam 1, MCP tools (implementation-ready)

Five read-only tools join the world tools in I's registry, each calling the handler its REST route
serves so the two cannot drift: `spec_list` (a project's specs), `spec_get`, `board_items` (board
tasks, `limit` ≤ 100), `approvals_pending`, `run_status`. Workspace scoping comes from the token's
principal; a foreign id looks nonexistent. Every result carries the §4 provenance fields.

### 2.2 Seam 3, run event stream (implementation-ready)

ADR-0013 fixes the vocabulary, the gated/observed mapping and the envelope. Interfaces:

- `forge_contracts.run_events.RunEvent` (the envelope) and `RunEventType` (the emitted names).
- `agent_run_event(id, workspace_id, run_id → agent_run, seq, type, enforcement, summary, payload,
  ts)`, unique `(run_id, seq)`, append-only.
- The worker attaches a listener to the run state's `add_step` and appends events as steps happen,
  plus `thread.started`, `artifact.created`, `cost.updated` and the terminal event from
  `run_agent_task`. Supervised multi-agent runs do not emit events yet (next: each sub-agent run
  under its own run id).
- `GET /agent/runs/{id}/events` (reader permission, workspace-scoped, foreign ids 404):
  `text/event-stream`, one SSE message per event (`id: <seq>`, `event: <type>`, `data: <json>`),
  replay after `Last-Event-ID` or `?after=`, poll for new rows about once a second, keep-alive
  every 15 s, close after the terminal event or after 30 minutes.
- Acceptance: a run's events arrive in `seq` order with the flag the ADR-0013 table gives; a
  reconnect with `Last-Event-ID` resumes without gaps or duplicates; a denied tool call appears as
  `tool.requested` (observed) then `permission.denied` (gated); secrets in tool output are redacted;
  an event-write failure does not fail the run.

## 3. Auth

Workspace-scoped personal access tokens (existing API keys with a `scope` narrowed to the four read
surfaces plus `approvals:decide`) or a device-code flow. Tervin reads the token from a named
environment variable — it stores no secrets and opens no listening port, so loopback OIDC is out.

## 4. Provenance

Every object forge sends carries `observed_by`, `recorded_at`, and `enforceable: bool`, so tervin's
honesty rule can render it as gated or observed rather than "unknown".

## 5. A single-user forge profile

A compose profile that runs api + worker + Postgres only (no MinIO, Caddy or Temporal), so a
developer can run forge beside tervin on one machine. Programme E (paved road) owns its UX.

## 6. Changes tervin needs (tracked in tervin's repo, local v3 tree)

- Spec 023 / #118: "consume forge when connected" instead of re-implementing forge designs in Rust.
- Spec 015: point the first outbound connector at forge; list it in `nothing_leaves_this_machine.rs`.
- Open decisions 6 (knowledge consent) and 7 (OIDC): resolved by forge-side consent and PAT auth.
- Specs 012–014 (coordinator, merge queue, knowledge): deferred in connected mode.

## 7. Acceptance criteria (when promoted to implementation-ready)

1. Tervin lists forge specs, approvals and world facts through MCP with no tervin code change.
2. A forge run renders live in tervin with each event marked gated or observed.
3. An approval answered in tervin resolves in forge with an audit entry naming tervin as the surface.
4. Disconnecting tervin leaves its local board usable; the next sync reports conflicts, never
   overwrites.
