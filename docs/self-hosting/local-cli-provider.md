# Local agent CLI provider (dev / self-host)

> **Development and self-host convenience only. This is not for a shared or
> production deployment.** It executes host binaries (`claude`/`codex`) on
> behalf of whatever can reach the bridge process; treat it the same as any
> other credentialed local dev tool, never as a service to expose beyond your
> own machine.

## What it is

Forge's model client is provider-agnostic BYOK (`FORGE_MODEL_PROVIDER=anthropic|openai`
plus an API key). This provider is a third option for local development and
self-hosting: instead of an API key, Forge drives a locally installed `claude`
or `codex` CLI as a **text oracle** — it sends a prompt, parses a structured
JSON envelope back, and treats that exactly like an API response. The CLI's
own tool use (Bash, Edit, Write, file access, web fetch, ...) is configured
off on every invocation: these CLIs are themselves autonomous coding agents,
and running one unconstrained *while Forge's own agent loop also acts* would
mean two agent loops mutating one working tree with no sandbox boundary
between them. All tool execution is meant to stay inside Forge's own audited
sandbox; the local CLI is meant to only ever propose text and (optionally)
which Forge tool to call next, via the JSON envelope. **What is proven vs.
what is configured differs by CLI — see "Verification status" below before
you pick one.**

This is meant for people who already pay for a `claude`/`codex` subscription
and want to try or run Forge without also provisioning a separate BYOK API
key.

## Why a bridge exists (the architecture constraint)

The Forge worker (and API) run inside Linux containers. The `claude`/`codex`
CLIs you have installed are host binaries (e.g. Mach-O/arm64 on macOS) — a
Linux container cannot execute them, and shipping a Linux build of each CLI
into the image is not something Forge controls or wants to depend on.

So when the container needs the local CLI, it does not spawn it directly.
Instead, you run a small HTTP bridge (`forge-cli-bridge`) **on the host**,
listening on loopback. The bridge spawns the CLI as a subprocess on your
behalf and returns the same request/response envelope
(`ModelRequest`/`ModelResponse`) the container would otherwise build itself.
The container reaches it over `host.docker.internal`, which resolves to the
host from inside the Docker network (verified: `docker exec
forge-dev-worker-1 getent hosts host.docker.internal` returns an address in
this dev stack; if it does not resolve in your environment, add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the affected service in
your compose file).

This is also why the bridge — not the container — is the trust boundary: your
CLI credentials (Claude/Codex login state) never leave the host or enter a
container. The bridge process sees only the request/response text, not your
credentials.

## Setup

1. **Have a supported CLI installed and authenticated on the host.**
   `claude` and/or `codex` on `PATH`, already logged in (`claude login` /
   `codex login`), with an active subscription. **The two are not equally
   proven** — `claude` has been exercised end to end (a real subprocess
   invocation, through the bridge, in a real agent run); `codex` support is
   implemented and unit-tested but has never been run against the live
   binary. See "Verification status" below before choosing.

2. **Start the bridge on the host**, in its own terminal:

   ```bash
   export FORGE_CLI_BRIDGE_TOKEN=$(openssl rand -hex 16)
   make cli-bridge
   ```

   `make cli-bridge` runs `uv run python -m forge_cli_bridge.main` —
   **not** `uvicorn forge_cli_bridge.main:app --host ... --port ...`. This
   matters: `main.py` binds the socket itself, from the same validated
   `BridgeSettings` object it built the app from, so the address it actually
   binds is the address its own loopback guard checked. Invoking the module
   through an external `uvicorn --host/--port` invocation instead bypasses
   that guard entirely (a security review on this project flagged exactly
   this shape as a bind-guard bypass: `BridgeSettings` never sees the
   `--host` an external ASGI runner is given, so a typo'd `--host 0.0.0.0`
   binds every interface while `/health` still reports the process as
   loopback-only). If you need an ASGI-managed deployment that owns its own
   bind configuration, `uvicorn forge_cli_bridge.main:app` still works as a
   plain ASGI import — that is not the default or the path this guide covers,
   and it is not an endorsement: on that path `main.py`'s bind guard never
   runs at all, so **you** are solely responsible for guaranteeing the
   socket stays loopback (whatever `--host`/`--port` you pass it), with
   nothing in this codebase checking that for you.

   By default this binds `127.0.0.1:8099`. Override with
   `FORGE_CLI_BRIDGE_HOST` / `FORGE_CLI_BRIDGE_PORT` (non-loopback requires
   the explicit `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1` opt-out — see
   "Security posture" below; you should not need this for a normal
   self-hosted single-machine setup).

3. **Confirm the bridge sees your CLI(s):**

   ```bash
   curl -fsS http://127.0.0.1:8099/health
   # {"status":"ok","clis":["claude"],"non_loopback_override_set":false}
   ```

   `/health` requires no auth. `clis` is empty if neither `claude` nor
   `codex` is on the bridge process's `PATH`.

4. **Add these three variables to `deploy/.env.dev`** (or your `.env` for a
   non-dev compose file) and re-run `make dev-up` / `scripts/dev.sh up` so the
   worker and API containers pick them up:

   ```bash
   FORGE_LOCAL_CLI_ENABLED=1
   FORGE_CLI_BRIDGE_URL=http://host.docker.internal:8099
   FORGE_CLI_BRIDGE_TOKEN=<the same token you exported above>
   ```

   Leave `FORGE_MODEL_PROVIDER` unset — `FORGE_LOCAL_CLI_ENABLED` is the
   auto-detect opt-in that only takes effect when no other provider is
   explicitly configured (see "Resolution order" below). `scripts/dev.sh`
   prints these same three lines automatically whenever it detects `claude`
   or `codex` on your `PATH` and no `FORGE_MODEL_PROVIDER` is set.

5. **Run an agent objective.** A run that previously returned `503 no model
   provider is configured` now proceeds through the bridge instead.

## Verification status

`claude` and `codex` are **not equally proven**, and the difference matters
when you pick `FORGE_LOCAL_CLI_PREFERENCE`. Both backends are implemented and
covered by the hermetic unit suite (argv construction, envelope parsing,
error mapping); the difference below is specifically about live, real-binary
verification.

| | `claude` | `codex` | `muse` |
| --- | --- | --- | --- |
| Implemented + unit-tested | Yes | Yes | Yes |
| Tool suppression *configured* | Yes — empty `--allowedTools`, an explicit `--disallowedTools` deny-list, `--permission-mode plan`, `--permission-prompts none` | Yes — `--sandbox read-only` | Yes — `--preset empty`, `--disable-shell`, `--disable-write`, `--disable-web-tools`, `--disable-request-user-input`, `--approval-mode untrusted`, and a fresh empty `--workspace` |
| Tool suppression *enforced end to end* | **Not yet exercised by a test** (flag *acceptance* only — see "Security posture" below) | **Not yet exercised by a test** (same caveat) | **Not yet exercised by a test** (same caveat) |
| Run against the real binary at all | **Yes** — this task's own end-to-end verification drove a real `claude` subprocess through the bridge in a real agent run | **No.** Argv construction, `--output-schema` handling, and stdin behavior are inferred from `codex exec --help` text, not from a live invocation | **Offline only.** The exact argv was run against the real binary with muse's offline `echo` provider: it is accepted, and the answer arrives as the `run.terminal.completed` event. `muse exec` cannot read stdin, so the prompt goes in a private (0600) temp file passed with `--prompt-file`. No live `meta` model call yet |
| Why | — | The operator's `codex` subscription quota has been exhausted since before this sub-project started; every live verification across every task used `claude` only | — |

Pick the CLI's model with `FORGE_LOCAL_CLI_MODEL` (for example
`muse-wonder-full-1.0` or `muse-spark-1.3`); it becomes that CLI's `--model`
flag (`-m` for codex). With the bridge, set it in the bridge's environment.

**If you set `FORGE_LOCAL_CLI_PREFERENCE=codex` (or let `codex` win auto-detect
because `claude` isn't installed), your own first run is the first real test
of that code path against a live `codex` binary — not just in your
environment, but at all.** Watch its first response carefully. This is not a
reason to avoid `codex`; the implementation is not known to be broken, only
unexercised. It is a reason to know which one you are trusting.

See `packages/agent-runtime/forge_agent/providers/local_cli/backends.py` —
`CodexCliBackend`'s docstring spells out exactly what is inferred-from-docs
vs. verified, and the module-level `TODO(local_cli behavioural verification)`
covers both backends' tool-suppression enforcement gap.

## Resolution order

`ModelClientConfig.from_env()` (`packages/agent-runtime/forge_agent/providers/config.py`)
resolves, in order:

1. `FORGE_MODEL_PROVIDER` explicitly set to `anthropic` or `openai` with a key
   present — that provider wins, local-CLI is not considered.
2. `FORGE_MODEL_PROVIDER=local_cli` — explicit opt-in; `FORGE_LOCAL_CLI_ENABLED`
   is not required (naming the provider outright is the opt-in).
3. `FORGE_MODEL_PROVIDER` unset **and** `FORGE_LOCAL_CLI_ENABLED=1` — auto-detect:
   uses `FORGE_CLI_BRIDGE_URL` if set (bridge transport), otherwise looks for
   `claude`/`codex` directly on the *current process's* `PATH` (only useful
   when that process is not containerized — i.e. not the dev/prod worker or
   API container, which cannot execute host binaries at all; **inside a
   container you always need the bridge**).
4. Nothing configured — the worker/API refuse the run (`503`) unless
   `FORGE_ALLOW_SCRIPTED_AGENT=1` is set for the offline canned-output stub.

`FORGE_LOCAL_CLI_PREFERENCE` (comma-separated, e.g. `claude,codex`) controls
which CLI is preferred when more than one is detected; it defaults to
`claude,codex`.

> **Set it where the bridge runs — not only in `deploy/.env.dev`.** On the
> bridge transport (the normal containerised setup), the *bridge* process is
> the one that spawns a CLI, so it is the only process whose
> `FORGE_LOCAL_CLI_PREFERENCE` decides which binary actually runs. The
> worker/API copy of the variable only labels the run (it becomes the
> `local-cli:<cli>` model string in cost accounting); the worker cannot
> execute a host binary, so it cannot act on the choice. Export it in the
> same shell you start the bridge from:
>
> ```bash
> export FORGE_CLI_BRIDGE_TOKEN=$(openssl rand -hex 16)
> export FORGE_LOCAL_CLI_PREFERENCE=codex   # optional; defaults to claude,codex
> make cli-bridge
> ```
>
> Set it in **both** places if you set it at all, so the label and the binary
> agree. The preference is the candidate set as well as the order (same
> semantics as `ModelClientConfig.from_env`): naming only a CLI that is not
> installed on the bridge's `PATH` yields `stop_reason: provider_unavailable`
> rather than a silent substitution of the other one. `GET /health` lists the
> detected CLIs in the configured order, so it shows you which one a run
> would pick.

## Security posture

| Control | Behavior |
| --- | --- |
| Bind address | `127.0.0.1` only, unless `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1` is explicitly set. Enforced both in `BridgeSettings.__post_init__` (construction time) and again as an `if: raise` guard in `main.py` right before the socket bind (not a bare `assert`, so it survives `python -O`). |
| Authentication | `/v1/complete` refuses to start the process at all without `FORGE_CLI_BRIDGE_TOKEN` set (`BridgeSettings.__post_init__` raises). Every call must carry `Authorization: Bearer <token>`, compared in constant time (`hmac.compare_digest`); a missing/wrong token is `401`. |
| CLI credentials | Never read or forwarded by the bridge. They stay in the host's keychain/CLI config and are used directly by the `claude`/`codex` subprocess the bridge spawns; the bridge itself only ever sees the request/response envelope text. |
| Startup banner | An unconditional warning-level log line restates "DEVELOPMENT / SELF-HOST CONVENIENCE ONLY" every time the process starts, not only when the non-loopback override is set. |
| Concurrency bound | At most `FORGE_CLI_BRIDGE_MAX_CONCURRENCY` (default 4) `/v1/complete` calls may spawn a CLI subprocess at once — each can run for up to `FORGE_LOCAL_CLI_TIMEOUT_S` (default 600s) and spends real subscription quota, so an unbounded burst is a plausible accident, not only an attack. |
| CLI tool use | **Configured** off on every invocation (empty `--allowedTools` plus an explicit `--disallowedTools` deny-list, `--permission-mode plan`, `--permission-prompts none` for `claude`; `--sandbox read-only` for `codex`) — the CLI is meant to be a text oracle only. Configuration has been verified to be *accepted* by the real `claude` CLI (it doesn't error on it); the `codex` flags have never been sent to the live binary at all (see "Verification status" above), so for `codex` even acceptance is unverified. **Enforcement has not yet been exercised end to end**: no test sends a prompt engineered to provoke a tool call and asserts none occurs (see the `TODO(local_cli behavioural verification)` in `packages/agent-runtime/forge_agent/providers/local_cli/backends.py`). Treat suppression as the intended, configured design, not as independently proven. |
| Stderr redaction | Raw CLI stderr is passed through the shared `forge_api` secret redactor before it ever reaches the host log, when that package is installed; otherwise the bridge logs a loud warning that redaction is unavailable rather than silently skipping it. |
| `/health` semantics | Reports detected CLIs and whether *this process's own* `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK` was set — it is not, and cannot be, a report of the actual bound socket (that depends on how the process was launched; see "Setup" above). Confirm real exposure with `lsof -i :8099` if you need to. |

Full detail: [`apps/cli-bridge/README.md`](../../apps/cli-bridge/README.md) and
the module docstrings in `apps/cli-bridge/forge_cli_bridge/{main,app,settings}.py`.

## Cost accounting

A local-CLI run consumes a paid *subscription*, not a metered API call — it
has real cost with no per-token price. The model string is namespaced
(`local-cli:claude`, `local-cli:codex`, or the bare `local-cli` fallback) so it
can never collide with a real provider's pricing row by exact-match lookup.
`UsageMeter` marks these runs `priced=False` rather than `cost_usd=0`, so a
local-CLI run is distinguishable from a run that genuinely cost nothing —
never silently reported as free.

## Troubleshooting

- **Worker/API still return `503`** — confirm the three variables landed in
  the *running* container (`docker compose -f deploy/docker-compose.dev.yml
  exec worker env | grep FORGE_LOCAL_CLI`), and that you re-ran `make dev-up`
  / `scripts/dev.sh up` after editing `deploy/.env.dev` (the compose services
  pick up env changes only on recreate, not on a bare restart).
- **`stop_reason: provider_unavailable`** — the bridge did not detect the CLI
  named in the response (or no CLI at all): check `curl
  http://127.0.0.1:8099/health` and that the CLI is on the *bridge process's*
  `PATH`, not just your interactive shell's.
- **`stop_reason: auth_error`** — the CLI itself reported it is not logged in;
  run `claude login` / `codex login` again on the host.
- **Worker container cannot resolve `host.docker.internal`** — on Docker
  Desktop (macOS/Windows) this resolves out of the box. On Linux Docker
  Engine, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the
  affected service.
