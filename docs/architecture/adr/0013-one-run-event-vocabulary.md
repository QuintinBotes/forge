# 0013. One run-event vocabulary with an enforceability flag

**Status:** Accepted
**Date:** 2026-11-07 (proposed); 2026-11-08 (accepted)

## Context

Tervin normalises every agent session into one append-only event vocabulary (dotted names such as
`thread.started`, `tool.requested`, `permission.denied`) and records, for every decision, who
actually decided: its own rules, or the agent runtime with tervin only watching. Its rule is that a
provider's approval must never be shown as though tervin gated it.

Forge records a run as a list of `Step`s (`plan`, `tool_call`, `observation`, `decision`, `message`,
`output`, `error`, `handoff`) written once, when the run ends. A client cannot follow a run live, and
cannot tell an action forge enforced from one it merely recorded.

## Decision

1. **Vocabulary.** `forge_contracts.run_events` defines `RunEvent`, re-implemented from tervin's
   published event names (no code copied). Forge emits this subset, and only these names:

   | Event | Emitted when | Enforcement |
   |---|---|---|
   | `thread.started` | the worker admits the run (repository resolved, workspace prepared) | gated |
   | `agent.message` | a `plan`, `message` or `output` step, or an observation with no tool call | observed |
   | `tool.requested` | the model asks for a tool (each `decision` step) | observed |
   | `permission.granted` / `permission.denied` / `permission.requested` | forge's policy gate allows, denies, or requires approval for that call | gated |
   | `tool.completed` | an allowed tool ran in the run's sandbox (its `tool_call` step) | gated |
   | `git.changed` | the `git_commit` tool committed in the run's worktree | gated |
   | `artifact.created` | the finalizer opened a pull request | gated |
   | `cost.updated` | token usage and derived cost at the end of a run | observed |
   | `thread.completed` / `thread.failed` | the run's terminal status is recorded (`failed` carries the stop reason, e.g. `budget_exhausted`) | observed |

   `gated` means forge's control plane decided or performed the action before it took effect (the
   policy gate, the declared tool belt in a sandbox, the finalizer's scoped token). `observed` means
   forge recorded something it did not control (model text, usage, an outcome). The mapping above is
   the only place the flag is set, so an event cannot claim enforcement forge did not have.

2. **Envelope.** Every event carries `run_id`, a per-run monotonically increasing `seq`, `type`,
   `ts`, a one-line human `summary`, `enforcement`, a small `payload`, and the provenance fields of
   ADR-0015 (`observed_by: "forge"`, `recorded_at`, `enforceable` = `enforcement == "gated"`).
   Payload text (tool arguments and output, messages) is redacted with the same scrubber as MCP
   results and truncated to 2,000 characters; the full step stays in the run record.

3. **Storage and delivery.** Events are appended to `agent_run_event` (unique `(run_id, seq)`,
   append-only, workspace-scoped) as the run progresses, through a listener on the run state's single
   `add_step` choke point, so the agent loop's logic does not change. `GET /agent/runs/{id}/events`
   serves them as Server-Sent Events: it replays after `Last-Event-ID` (or `?after=`), then polls
   for new rows until it has sent a terminal event, with a keep-alive comment every 15 seconds and a
   bounded stream lifetime after which the client reconnects with its last id. Writing an event never
   fails the run; a write failure is logged.

4. Later seams reuse the vocabulary: the runner protocol (spec L seam 5) streams these events from a
   remote runner, with `enforcement` set by whichever side actually gated each action.

## Consequences

- Tervin renders a forge run as a thread without a translation layer, and its honesty rule holds
  across the boundary.
- Any client (CI, scripts, the web UI) can follow a run live through one endpoint.
- Adding an event type is a contract change: it needs a row in the table above and a stated
  enforcement.
