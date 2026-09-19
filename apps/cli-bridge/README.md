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
  attack surface to anything that can reach the host.
* **Refuses to start without `FORGE_CLI_BRIDGE_TOKEN`.** Every `/v1/complete`
  call must carry `Authorization: Bearer <token>`; a missing or wrong token is
  a `401`.
* **Never reads or forwards CLI credentials.** Those stay in the host's
  keychain/config, used directly by the `claude`/`codex` CLI process the
  bridge spawns — the bridge itself sees only the request/response envelope.
* Both refusals above happen at construction time (`BridgeSettings.__post_init__`),
  loudly, before the server ever binds a socket.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FORGE_CLI_BRIDGE_TOKEN` | *(required)* | Bearer token every `/v1/complete` caller must present. |
| `FORGE_CLI_BRIDGE_HOST` | `127.0.0.1` | Bind address. Non-loopback requires the override below. |
| `FORGE_CLI_BRIDGE_PORT` | `8099` | Bind port. |
| `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK` | unset | Set to `1`/`true`/`yes`/`on` to allow a non-loopback `FORGE_CLI_BRIDGE_HOST`. |
| `FORGE_LOCAL_CLI_TIMEOUT_S` | `600` | Per-request CLI subprocess timeout, in seconds. |

## Endpoints

* `GET /health` — returns detected CLIs and whether the bridge is loopback-only.
  No authentication required.
* `POST /v1/complete` — accepts a `ModelRequest` body, returns a `ModelResponse`.
  Requires `Authorization: Bearer <FORGE_CLI_BRIDGE_TOKEN>`.

## Running it

```bash
FORGE_CLI_BRIDGE_TOKEN=$(openssl rand -hex 32) \
  uv run uvicorn forge_cli_bridge.main:app --host 127.0.0.1 --port 8099
```

Part of the [Forge](../../README.md) monorepo. Apache-2.0 licensed.
