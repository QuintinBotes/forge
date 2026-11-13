# forge-cli-bridge

Loopback HTTP bridge exposing the host's locally installed agent CLIs (`claude`,
`codex`) to the containerised Forge worker.

> **Development and self-host convenience only. This is not for a shared or
> production deployment.** It executes host binaries on behalf of whatever can
> reach it; treat it the same as any other credentialed local dev tool.

## Why this exists

The Forge worker runs inside a Linux container and cannot execute the host's
`claude`/`codex` CLI binaries (Mach-O/arm64 on macOS). This bridge runs on the
host, listens on loopback, and forwards `ModelRequest`/`ModelResponse` traffic
to `LocalCliModelClient`, which drives the CLI as a subprocess. The container
reaches it over `host.docker.internal` (or an equivalent loopback route) instead
of needing its own copy of the CLI.

## Security model

* **Binds `127.0.0.1` only** unless `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1` is
  explicitly set. Setting that variable is a loud, deliberate opt-out — the
  bridge executes host binaries, so exposing it beyond loopback widens the
  attack surface to anything that can reach the host. Run it with
  `python -m forge_cli_bridge.main` (see "Running it" below) so the address it
  actually binds is the SAME validated value the guard checked — not a second,
  independently-typed `--host` flag on an ASGI server's own command line that
  the guard never sees.
* **Refuses to start without `FORGE_CLI_BRIDGE_TOKEN`.** Every `/v1/complete`
  call must carry `Authorization: Bearer <token>` (compared in constant time);
  a missing or wrong token is a `401`.
* **Never reads or forwards CLI credentials.** Those stay in the host's
  keychain/config, used directly by the `claude`/`codex` CLI process the
  bridge spawns — the bridge itself sees only the request/response envelope.
* Both refusals above happen at construction time (`BridgeSettings.__post_init__`),
  loudly, before the server ever binds a socket — and the process logs an
  unconditional startup banner restating that this is development/self-host
  only, every time it starts, not only when the non-loopback override is set.
* **Bounded concurrency.** At most `max_concurrent_completions` (default 4)
  `/v1/complete` calls may spawn a CLI subprocess at once; each can run for up
  to `timeout_s` (default 600s) and spends real subscription quota, so an
  unbounded burst is a plausible accident, not just an attack.
* Raw CLI stderr is passed through a redactor before it ever reaches the host
  log (the shared `forge_api` secret redactor when available, else identity).

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FORGE_CLI_BRIDGE_TOKEN` | *(required)* | Bearer token every `/v1/complete` caller must present. |
| `FORGE_CLI_BRIDGE_HOST` | `127.0.0.1` | Bind address. Non-loopback requires the override below. |
| `FORGE_CLI_BRIDGE_PORT` | `8099` | Bind port. |
| `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK` | unset | Set to `1`/`true`/`yes`/`on` to allow a non-loopback `FORGE_CLI_BRIDGE_HOST`. |
| `FORGE_LOCAL_CLI_TIMEOUT_S` | `600` | Per-request CLI subprocess timeout, in seconds. |
| `FORGE_CLI_BRIDGE_MAX_CONCURRENCY` | `4` | Max concurrent `/v1/complete` CLI subprocess spawns. |
| `FORGE_LOCAL_CLI_PREFERENCE` | `claude,codex` | Comma-separated order of CLIs to try. Read from **this** process's environment: the bridge is what spawns the binary, so this is the copy that decides which one runs (the worker's copy only labels the run). Also the candidate set — naming only an uninstalled CLI yields `provider_unavailable`. |

## Endpoints

* `GET /health` — returns detected CLIs and `non_loopback_override_set`: whether
  `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK` was set on THIS process's config. This
  is not a report of the actual bound socket — the process cannot see that
  when launched via `uvicorn forge_cli_bridge.main:app --host ...` (see
  "Running it" below); check the real listening socket (e.g. `lsof -i :8099`)
  if you need to confirm actual exposure. No authentication required.
* `POST /v1/complete` — accepts a `ModelRequest` body, returns a `ModelResponse`.
  Requires `Authorization: Bearer <FORGE_CLI_BRIDGE_TOKEN>`.

## Running it

```bash
FORGE_CLI_BRIDGE_TOKEN=$(openssl rand -hex 32) \
  uv run python -m forge_cli_bridge.main
```

This is the supported way to run the bridge: `main.py` binds `uvicorn` itself
from the same validated `BridgeSettings` object it built (host/port/loopback
guard all come from one place). An ASGI-managed deployment that instead
imports `forge_cli_bridge.main:app` directly (e.g. `uvicorn
forge_cli_bridge.main:app --host ... --port ...`) is bound by whatever
`--host`/`--port` that server is given, same as any other ASGI app — that
path is for process managers that own their own bind configuration, not a
substitute for `python -m forge_cli_bridge.main` as the default way to run
this locally.

Part of the [Forge](../../README.md) monorepo. Apache-2.0 licensed.
