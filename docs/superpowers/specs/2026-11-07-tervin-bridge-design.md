# L — Tervin bridge

**Date:** 2026-11-07
**Status:** DRAFT — scope and interfaces only
**Depends on:** I (forge MCP server), G (real runs and their events)
**Blocks:** nothing in forge
**Decisions:** ADR-0002 (forge is the system of record, tervin its desktop face), ADR-0013 (one
run-event vocabulary, proposed), ADR-0015 (desktop integration contract)

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
