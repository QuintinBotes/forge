# F — Local-CLI model provider

**Date:** 2026-09-19
**Status:** Approved, implementation-ready
**Depends on:** nothing
**Blocks:** dogfooding of A–E (not their implementation)

---

## 1. Problem

Forge requires a BYOK API key to run agents. With none configured, `ModelClientConfig.from_env`
returns `None`, the worker falls back to `ScriptedModelClient`, and `FORGE_ALLOW_SCRIPTED_AGENT`
defaults false, so agent runs are refused with HTTP 503.

Many self-hosters — including this repository's own operator — already have authenticated agent
CLIs installed (`claude`, `codex`) with subscription entitlements. Requiring a separate metered
API key to evaluate Forge is an adoption tax for no technical reason.

**Goal:** when no API key is configured, detect locally installed agent CLIs and drive them as a
model provider.

## 2. The architecture constraint

Measured on this machine, 2026-09-19:

| | |
|---|---|
| Host | `Darwin arm64` |
| `claude` binary | `Mach-O 64-bit executable arm64` at `~/.local/bin/claude` |
| Worker container | `Linux aarch64` |
| CLI visibility from worker | **none** |

A Mach-O binary cannot execute under Linux. Bind-mounting the host CLI into the worker container
is therefore impossible, not merely awkward. Any design premised on "the worker shells out to the
local CLI" is void while the worker is containerised.

**Resolution:** a host-side bridge over loopback.

```
host (darwin/arm64)                    container (linux/aarch64)

  claude -p            ◀─┐
  codex exec           ◀─┤ subprocess
                         │
  forge-cli-bridge       │
  127.0.0.1:8099  ◀──────┴──── HTTP ────  worker
   + shared token                         LocalCliBridgeClient
   + tools disabled                       (implements ModelClient)
```

CLI credentials never enter a container. No Linux CLI build is required. The bridge is the only
new process an operator must run, and it is optional — absent it, behaviour is exactly as today.

## 3. The tool-calling problem

Forge's agent loop is tool-call driven. `packages/agent-runtime/forge_agent/runtime.py:167-172`
reads `response.tool_calls` and dispatches each one. A text-only wrapper around a CLI would return
`tool_calls=[]` forever and the agent would never act — the provider would appear to work while
accomplishing nothing.

So the provider must produce `ModelToolCall`s. Both CLIs can be made to, with differing guarantees:

| CLI | Mechanism | Guarantee |
|---|---|---|
| `codex` | `codex exec --output-schema <FILE> --json` | **Schema-enforced.** The final response conforms to a supplied JSON Schema |
| `claude` | `claude -p --output-format json` + envelope instruction in the system prompt | **Prompt-instructed.** Valid JSON result wrapper; inner shape depends on instruction adherence |

The envelope both are asked to produce:

```json
{
  "content": "assistant prose, may be empty",
  "tool_calls": [
    { "name": "read_file", "arguments": { "path": "app/main.py" } }
  ],
  "stop_reason": "tool_use" | "end_turn"
}
```

Reliability measures, in order of application:

1. **Schema enforcement** where the CLI supports it (codex).
2. **Strict parse** — the envelope is validated against the `ModelResponse` shape. Unparseable
   output is not coerced.
3. **One repair retry** — on parse failure, re-invoke once with the malformed output and a repair
   instruction appended.
4. **Fail loud** — a second failure yields `ModelResponse(stop_reason="parse_error")`. The agent
   run fails visibly. It must never silently return empty `tool_calls`, because that is
   indistinguishable from "the model chose to do nothing".

### Tool suppression

The CLIs are themselves agents. Left unconstrained, `claude -p` would plan and edit files on the
host while Forge's own loop also tried to act — two agent loops, one working tree, no sandbox.

The provider therefore always disables the CLI's own tool use, in layers, most restrictive first:

- `claude`: `--allowedTools ""` — an allow-list permitting nothing, which is **fail-closed**;
  then `--disallowedTools <known tools>` as a second layer; then `--permission-mode plan` and
  `--permission-prompts none`. Never `--dangerously-skip-permissions`.
- `codex`: `--sandbox read-only`, and never `--dangerously-bypass-approvals-and-sandbox`

> **Revised 2026-09-19 during implementation.** This section originally specified only
> `--disallowedTools`. Review found that shape **fail-open**: the list hardcodes eleven tool
> names, so the day the CLI ships a twelfth it is silently permitted. Since tool suppression is
> this provider's security property, the primary mechanism became an empty allow-list, which
> denies by construction and cannot go stale. The deny-list is retained behind it. The empty
> allow-list was verified accepted by the real CLI before adoption.

Two flags in this argv are **variadic** — `--allowedTools` and `--disallowedTools` both take
`<tools...>` and greedily consume bare tokens until the next `--`-prefixed flag. The trailing
user prompt is a bare positional, so the ordering above is load-bearing: a reorder placing either
variadic flag last would silently swallow the prompt as a tool name and send the CLI no prompt at
all. A regression test asserts the ordering invariant.

**What is demonstrated, and what is not.** A live call confirmed the CLI *accepts* this flag
combination and returned `permission_denials: []`. That is acceptance, not enforcement: no test
yet provokes a tool-use attempt and observes it denied end to end. Until one does, treat
suppression as configured-and-plausible rather than proven.

The CLI is used strictly as a text oracle. All tool execution stays inside Forge's sandbox, where
it is audited.

## 4. Components

### 4.1 `packages/agent-runtime/forge_agent/providers/`

| File | Responsibility |
|---|---|
| `local_cli/detect.py` | Discover installed CLIs on `PATH`; return `[]` on any error. Never raises |
| `local_cli/backends.py` | `ClaudeCliBackend`, `CodexCliBackend` — argv construction, subprocess invocation, timeout, non-zero exit handling |
| `local_cli/client.py` | `LocalCliModelClient` implementing `ModelClient` (`complete`, `stream`) |
| `bridge_client.py` | `LocalCliBridgeClient` implementing `ModelClient` over HTTP to the bridge |

`translate.py` gains two pure functions beside the existing SDK translators, keeping all
request/response shaping in one fixture-testable module:

- `build_local_cli_prompt(request: ModelRequest) -> tuple[str, str]` → `(system, user)`
- `parse_local_cli_response(raw: str) -> ModelResponse`

### 4.2 `apps/cli-bridge/`

A minimal FastAPI app, following the existing `apps/mcp-gateway` layout.

- `POST /v1/complete` — body is a serialised `ModelRequest`, response a serialised `ModelResponse`
- `GET /health` — liveness, and reports which CLIs were detected
- Binds `127.0.0.1` only. Binding any other interface requires an explicit
  `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1` and logs a warning on every request.
- Requires `Authorization: Bearer <FORGE_CLI_BRIDGE_TOKEN>`; refuses to start without a token.
- Started by `make cli-bridge`, and by `scripts/dev.sh up` when CLIs are detected.

### 4.3 Configuration

`ProviderName` gains `local_cli`.

`ModelClientConfig` currently requires `api_key: str`. Local CLI auth lives in the CLI's own
credential store, so the field becomes `api_key: str = field(repr=False, default="")` and the
factory validates that a key is present *for the providers that need one*. This keeps the
never-log-the-key property intact.

New environment contract:

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_LOCAL_CLI_ENABLED` | `false` | Master opt-in for auto-detect |
| `FORGE_LOCAL_CLI_PREFERENCE` | `claude,codex` | Order to try detected CLIs |
| `FORGE_CLI_BRIDGE_URL` | unset | e.g. `http://host.docker.internal:8099` |
| `FORGE_CLI_BRIDGE_TOKEN` | unset | Shared secret; required by the bridge |
| `FORGE_LOCAL_CLI_TIMEOUT_S` | `600` | Per-invocation timeout |

### 4.4 Resolution order

Implemented in `ModelClientConfig.from_env`, unchanged in its never-raise contract:

1. `FORGE_MODEL_PROVIDER` set and valid → use it (existing behaviour, untouched)
2. A provider-native key present (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) → use it
3. `FORGE_LOCAL_CLI_ENABLED` and bridge reachable, or a CLI on `PATH` → `local_cli`
4. Otherwise → `None` (scripted fallback / 503, exactly as today)

Opt-in at step 3 is deliberate. Silently routing prompts to a local CLI — spending someone's
subscription quota and executing a binary they did not nominate — is not an acceptable default.

## 5. Cost metering

A CLI subscription has no per-token price, so `forge_obs.cost` cannot price these runs. The
existing `COST_UNPRICED_MODEL_BEHAVIOR=warn` path already covers exactly this case and is reused
unchanged.

Where a CLI reports token usage in its JSON envelope it is recorded as `TokenUsage` for volume
accounting, explicitly with a zero monetary figure and an `unpriced` marker — never as a cost of
zero, which would corrupt sub-project D's cost-per-change arithmetic by making local runs look
free when they consume a paid subscription.

## 6. Security posture

Recorded explicitly because this feature executes host binaries on behalf of a containerised
service.

| Risk | Mitigation |
|---|---|
| Container reaches host executables | Bridge is loopback-only, token-authenticated, refuses to start without a token |
| CLI acts as an agent on the host | Tools disabled and read-only sandbox enforced on every invocation; never bypassed |
| Prompt injection reaching a host CLI | CLI cannot act (tools off), so injection yields text only; that text re-enters Forge's normal sandboxed loop |
| Credential leakage | CLI credentials stay in the host keychain; the bridge never reads or forwards them; `api_key` stays `repr=False` |
| Silent quota spend | Off by default; requires explicit `FORGE_LOCAL_CLI_ENABLED` |
| Unbounded runtime | Per-invocation timeout, subprocess killed on expiry |

The bridge is a development and self-host convenience. It is **not** recommended for a shared or
production deployment, and its README and startup banner must say so.

## 7. Error handling

`LocalCliModelClient` never raises out of `complete()`. Every failure becomes a `ModelResponse`
with a diagnostic `stop_reason`:

| Condition | `stop_reason` |
|---|---|
| CLI not found | `provider_unavailable` |
| Non-zero exit | `provider_error` |
| Timeout | `timeout` |
| Unparseable after one repair retry | `parse_error` |
| CLI reported auth failure | `auth_error` |
| Argument list too large for `execve` (`E2BIG`) | `input_too_large` |

`LocalCliBridgeClient` maps transport failures (connection refused, 401, 5xx) onto the same set,
so the worker cannot tell — and need not care — which transport was used.

This vocabulary has exactly one definition, `forge_contracts.ProviderFailureStopReason` (and its
derived `PROVIDER_FAILURE_STOP_REASONS` set), which the providers emit from and the agent runtime
matches against. A response carrying one of these values **fails the run**: `AgentRunner` ends the
turn with `RunStatus.FAILED` and an error naming the `stop_reason`, never an empty `SUCCEEDED`
(§3's "fail loud", AC6). The SDK-backed providers are unaffected — they raise `ModelClientError`
rather than returning any of these values.

## 8. Testing

| Layer | Test |
|---|---|
| `translate.py` | Pure fixture tests: recorded CLI outputs → expected `ModelResponse`. No subprocess, no network. Includes malformed-output cases asserting `parse_error` rather than empty `tool_calls` |
| `detect.py` | Fake `PATH` fixtures; asserts empty list and no raise when nothing is installed |
| `backends.py` | Subprocess invocation asserted via a fake runner: argv must always contain the tool-suppression flags. A backend that omits them fails the test |
| `client.py` | Every error condition in §7 produces the correct `stop_reason` |
| bridge | FastAPI `TestClient`: 401 without token, 403 on non-loopback without the override, health reports detected CLIs |
| integration (`live_local_cli` marker) | Real CLI invocation, opt-in only, excluded from default CI like the other `live_*` lanes |

Test file basenames must be repo-unique — this repo collects the whole tree with `prepend` import
mode, and a duplicate basename fails collection with exit 2 across unrelated jobs. Planned names:
`test_local_cli_detect.py`, `test_local_cli_backends.py`, `test_local_cli_client.py`,
`test_local_cli_translate.py`, `test_cli_bridge_app.py`.

## 9. Acceptance criteria

1. With no API key and no opt-in, behaviour is byte-identical to today (503, scripted fallback).
2. With `FORGE_LOCAL_CLI_ENABLED=1` and a reachable bridge, an agent run completes end to end
   against the real `claude` CLI and produces at least one executed tool call.
3. The same run works against `codex` with `--output-schema` enforcement.
4. Every CLI invocation carries tool-suppression flags, asserted by test.
5. The bridge refuses to start without a token and refuses non-loopback binding without the
   explicit override.
6. A malformed CLI response surfaces as `parse_error`, never as a silent no-op.
7. Local runs appear in cost analytics marked `unpriced`, never as zero cost.
8. `make lint`, `make typecheck`, and the full suite pass.

## 10. Out of scope

- Streaming. `stream()` is implemented as a single-chunk yield over `complete()`. Real incremental
  streaming via `--output-format stream-json` is a later refinement.
- Any CLI beyond `claude` and `codex`. The backend interface admits more; none are specified here.
- Running the bridge anywhere other than an operator's own machine.
- Prompt caching. The CLIs manage their own; Forge's `prompt_cache` flag is ignored for this
  provider.
