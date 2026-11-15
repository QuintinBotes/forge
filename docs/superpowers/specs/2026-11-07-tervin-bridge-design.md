# L — Tervin bridge

**Date:** 2026-11-07
**Status:** Seams 1–4 built; seams 5–6 implementation-ready (§6b, §6c), not built
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

## 6a. Seam 4 decisions (2026-11-08)

Found while mapping seam 4: an API key not bound to a person could decide approvals (it resolved
to a "user" that does not exist, against ADR-0005), key scopes were not enforced anywhere, gates
opened by the engines, the worker and the SLA sweep never reached `/ws`, and approval audit rows
carried no workspace, so the audit query never returned them.
- **Transport.** `/ws` (ADR-0015), not a new stream. Non-browser clients authenticate with
  `Authorization: Bearer` (or `X-API-Key`). A socket authenticated by a narrowed key, or by any
  API key sent in a header, closes after 30 minutes and never outlives the key, so a revoked key
  stops receiving and the client reconnects; the web's `?token=` socket with its unscoped key, and
  session sockets, stay open.
- **Events.** `approval.requested`, `approval.decided` (final statuses only) and a new
  `approval.updated` (still pending: escalated, partially approved, routed). Payloads carry
  identifiers, gate, title, risk, expiry and, on decisions, `decided_by` and `surface`; never
  `gate_payload` (clients read `/approvals/{id}/context`). Every `/ws` event carries
  `observed_by`, `recorded_at` and `enforceable` (true for gates whose resolution hook acts:
  spec, plan, pr, deploy, policy_override). Every path that creates or changes a gate publishes:
  the API, the V1 engine, the Temporal worker and the SLA sweep (through the Redis channel the API
  relays, published after commit), and station gates.
- **Who can decide.** Only a person: a session, or an API key bound to a person (`user_id` set).
  A bound key with no scopes (the web's pasted key, SSO login keys, the seed's person-bound
  bootstrap key) acts as that person; a bound narrowed key decides only with `approvals:decide`.
  Unbound keys are machine principals and are refused; agent-runner tokens are agents. At decision
  time a key decides with the lower of its own role and its owner's current role (`User.role` and
  the owner's workspace grants); a missing or inactive owner, or one whose workspace grants have
  all expired, is refused, so demoting or deactivating someone takes effect on their keys' next
  decision. Removing a person is deactivation (which also revokes their keys): deleting their
  workspace grant alone leaves `User.role` in force, because seed, SSO and SCIM provisioning write
  no grants. A token is minted only by a credential holding its owner's full current role. A real session login for the web
  is a follow-up; until then the web's person-bound key is the person.
- **Personal tokens.** Minted by the person themselves (`POST /auth/personal-tokens`) from a
  session or their own person-bound unscoped key (the web's credential); narrowed and unbound keys
  are refused. A token names a surface (`tervin` or `cli`), carries `read` and optionally
  `approvals:decide`, and expires (90 days by default). It is stored with role viewer (scopes
  kept), so code that ignores scopes fails closed: it is a viewer everywhere else, and F30 sees only
  a workspace viewer grant, never its owner's grants or teams. It decides with its owner's current
  role. Only a key bound to a person can carry `approvals:decide`. Keys an SSO login mints carry
  the surface `web`.
- **Surface.** Taken only from the credential (the token's surface, `web` for a session, `system`
  for the sweep), never from a request header, and recorded with the key id in the decision's audit
  metadata. It is honest about the credential, not the binary; the key id keeps it traceable and
  revocable.
- **Resume.** No sequence numbers: a client opens `/ws`, buffers, fetches
  `GET /approvals?status=pending&mine=true`, applies buffered events by id (a final status wins),
  re-fetches on `approval.updated`, and treats a 409 on its decision as decided elsewhere.
- **Tervin's side.** Risk words are sent as forge's own; tervin maps them and labels non-enforceable
  gates as recorded only. A WebSocket client is a new dependency and a new outbound entry for tervin.

## 6b. Seam 5 decisions (2026-11-09)

A runner is a process on a person's machine (tervin first) that executes an agent run behind its
own permission rules and reports back. Forge cannot observe that machine. So everything a runner
sends is recorded as reported, and nothing forge enforces depends on a runner's claim.

- **Credential.** A personal token (§6a) with a new scope, `runs:execute`. The allowed scope sets
  become `read`, `read + approvals:decide` and `read + runs:execute`. The last two never share a
  token, so a credential a local agent could reach cannot decide a gate. A session or a
  person-bound unscoped key may also use the runner routes. Unbound keys and agent-runner keys are
  refused: a runner run needs an accountable person. At every call the owner's current role must
  grant `RUN_AGENT`, so demoting or deactivating someone stops their runner at its next call.
- **Registration.** `POST /runners {name, capabilities}` creates or returns the caller's runner
  (unique per owner and name; the surface comes from the credential). `GET /runners` lists them.
  `DELETE /runners/{id}`, by the owner or an admin, revokes one.
- **Starting a run.** The runner starts it: `POST /runners/{id}/runs {task_id}` validates the task
  as `POST /agent/runs` does (same handler, same one-active-run rule), creates the run with
  `executor = runner`, never enqueues it, and leases it to the caller in the same transaction. The
  answer carries the run id, the task snapshot, the repo target with the run branch
  `forge/run-<hex>`, the assembled context bundle (J §2.1) and the token budget. The budget is
  advisory: forge cannot stop a model call on another machine. Assigning a run to someone else's
  runner is not possible; assigning to one's own from the web is a follow-up.
- **Lease.** A lease lasts 120 seconds. `POST /runners/{id}/runs/{run}/heartbeat`, and every
  events or result call, extends it. A sweep fails a run whose lease expired (`lease_expired`) and
  emits its terminal event, so a sleeping laptop ends its run. A call after expiry is 409
  `lease_expired`.
- **Events.** `POST /runners/{id}/runs/{run}/events` appends at most 100 events per call. A runner
  may send only the step-level types: `agent.message`, `tool.requested`, `permission.requested`,
  `permission.granted`, `permission.denied`, `tool.completed`, `git.changed`, `cost.updated`.
  Forge emits `thread.started` (at lease), `artifact.created` and the terminal event itself. Forge
  assigns `seq` and `recorded_at`, and redacts and truncates as for its own events. Each event
  carries a client `event_id`, so a retried batch never duplicates.
- **Who gated.** Every event gains `gated_by`: `forge` or `runner`. A runner states `enforcement`
  per event. Forge accepts `gated` only for the types ADR-0013 marks gated, and any event may be
  lowered to `observed` (tervin watching a provider's own approval). `observed_by` names the
  runner's surface. `enforceable` is true only when forge gated the action. A client therefore
  reads a runner event as "gated by tervin, reported to forge", never as enforced by forge.
- **Result.** `POST /runners/{id}/runs/{run}/result` is terminal and idempotent (the same body
  again is accepted; a different one is 409). It carries the status, a summary, the model and token
  usage, the branch and head sha, and the runner's `rules_digest`. Usage is recorded as unpriced
  and self-reported: metered, never free, and never trusted as a price.
- **Storage.** A `runner` table. `agent_run` gains `executor`, `runner_id` and `lease_expires_at`.
  `agent_run_event` gains `gated_by`, `observed_by` and `client_event_id`. The event writer moves
  out of the worker into a package the API can import, and takes a per-run lock instead of
  dropping an event on a sequence conflict, because a run now has more than one writer.

Decided now, built in the next wave (5b):
- **Delivery.** The runner's owner pushes `forge/run-<hex>` with their own repository access.
  Forge then checks, through its App token, that the branch head is the reported sha, and opens the
  draft pull request itself. This amends ADR-0008 for runner runs: the push is the owner's, the
  pull request is forge's.
- **Attestation.** Forge alone signs. A runner never holds a key. The attestation uses a v2
  predicate that names the runner, its `rules_digest` and `self_reported: true`.
- **Open, for the owner to rule on:** whether the person whose runner executed a run may decide
  that run's `pr` gate. Forbidding it is the stricter reading of ADR-0005, and it makes a
  one-person workspace unable to merge runner work.

## 6c. Seam 6 decisions (2026-11-09)

- **Shape.** `forge-cli acp` runs forge's native agent loop in-process on the person's machine and
  speaks the Agent Client Protocol on stdio (newline-delimited JSON-RPC 2.0), so tervin can drive
  forge as one more ACP agent. The ACP version is pinned under ADR-0016.
- **Methods.** `initialize`, `session/new`, `session/prompt` and `session/cancel` in; `session/update`
  out; and `session/request_permission` before every tool call.
- **Two gates, forge's first.** Forge's policy gate evaluates each call. A forge denial is final
  and is never offered to the client. An allowed call is then put to the client; a rejection, a
  cancelled request, a malformed answer or a closed pipe denies it. Nothing runs unasked.
- **Tools.** The role's tool belt over the session's working directory. No worktree is created,
  and there is no push or pull-request tool, as on every other path (ADR-0014).
- **Recording.** With `FORGE_URL`, a `runs:execute` token named by `--token-env` and `--task`, the
  shim registers as a runner and records the run through seam 5, with `gated_by: runner` on what
  the client decided and `gated_by: forge` never claimed. Without them the run is local and
  unrecorded, and the shim says so on stderr.
- **Packaging.** The shim builds its runner from `forge_agent`; the CLI never imports the worker.

## 7. Acceptance criteria (when promoted to implementation-ready)

1. Tervin lists forge specs, approvals and world facts through MCP with no tervin code change.
2. A forge run renders live in tervin with each event marked gated or observed.
3. An approval answered in tervin resolves in forge with an audit entry naming tervin as the surface.
4. Disconnecting tervin leaves its local board usable; the next sync reports conflicts, never
   overwrites.
