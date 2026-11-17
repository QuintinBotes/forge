# MCP conformance

SCF's own MCP server (`POST /mcp`, spec I) speaks MCP revision `2026-07-28` and still serves
clients on the handshake-era revisions `2025-06-18` and `2025-03-26`. This page says how to run
the published MCP conformance suite against it and records one run.

| Pin | Value | Where |
|---|---|---|
| Protocol revision | `2026-07-28`, marked Current on the MCP versioning page when checked on 2026-11-17 | `CURRENT_VERSION` in `apps/api/scf_api/mcp_server/protocol.py`, ADR-0016 |
| Conformance suite | `@modelcontextprotocol/conformance` `0.2.0-alpha.12`, checked on npm on 2026-11-17 | `SUITE_VERSION` in `scripts/mcp-conformance.sh` |
| Expected failures | one entry per failing scenario or check, each with a reason | `scripts/mcp-conformance-baseline.yaml` |

On 2026-11-17 the npm `latest` tag was `0.1.16`, which has no `2026-07-28` scenarios. The
prerelease line does: `0.2.0-alpha.12` was its newest release and ships the frozen
`requirements/2026-07-28.yaml`, the list of scenarios a server must pass for that revision.
`tests/test_mcp_conformance_runner.py` fails if the script stops pinning one exact version or the
revision drifts from `CURRENT_VERSION`, and if a baseline entry has no reason.

## Run it

You need Node 20 or later and access to the npm registry. Point the script at an SCF MCP endpoint
and give it an API key:

```bash
SCF_MCP_TOKEN=<SCF API key> scripts/mcp-conformance.sh http://localhost:8000/mcp -o results
```

- The script runs `server --requirements 2026-07-28`: exactly the server scenarios that revision
  requires, plus the ones the suite runs without scoring (the Tasks extension and three pending
  scenarios). Extra arguments go to the suite; `-o <dir>` saves each scenario's `checks.json`.
- The suite sends no credentials. The script starts a small proxy on `127.0.0.1` (port
  `SCF_MCP_PROXY_PORT`, default `8765`) that adds `Authorization: Bearer $SCF_MCP_TOKEN` to each
  request and forwards it to the URL. The proxy stops when the script exits.
- The suite sends a few hundred requests in a burst. With the default rate limit
  (`FORGE_RATELIMIT_RPM=120`, `FORGE_RATELIMIT_BURST=60`) SCF answers some of them with HTTP 429
  and scenarios fail for that reason alone. Run it against an instance with a higher limit.
- Exit status is the suite's: `0` when every failure is in the baseline, `1` for a failure that
  is not, and `1` for a baseline entry that now passes (remove that entry).

## Recorded run

| | |
|---|---|
| Date | 2026-11-17 |
| Suite | `@modelcontextprotocol/conformance@0.2.0-alpha.12`, `server --requirements 2026-07-28` |
| Revision | `2026-07-28` |
| SCF | branch `roadmap/210` (issue #210) on top of `c67b92a` |
| Result | exit `0`: 50 scenarios, 95 checks passed, 71 failed, 4 warnings, 7 skipped; every failure and warning is in the baseline |

How SCF was run: the API app from this branch under uvicorn on `127.0.0.1`, with its real
API-key authentication (a member key minted in the in-memory key store), its real tool registry,
an in-memory SQLite database holding one workspace, and `FORGE_RATELIMIT_RPM=100000`,
`FORGE_RATELIMIT_BURST=10000`. It was not the Docker Compose stack, so Caddy and Postgres were
not in the path. A first attempt at the default rate limit got HTTP 429 responses and is not
recorded here.

What passed in full: `tools-list` (including the same order across repeated calls),
`http-header-validation` (14 checks: wrong, missing and case-changed `Mcp-Method` and `Mcp-Name`
values are refused with HTTP 400 and `-32020`), `server-sse-multiple-streams`,
`input-required-result-unsupported-methods` and `input-required-result-validate-input`.
`server-stateless` passed 21 of 25 checks, among them `server/discover`, server identity in
`_meta`, `-32602` with HTTP 400 for incomplete `_meta`, `-32022` and `-32020` with HTTP 400,
and HTTP 404 with `-32601` for `initialize`, `ping` and unknown methods. `caching` passed for
`tools/list`. `dns-rebinding-protection` refused a foreign `Origin`.

Checks per scenario (warnings are counted in neither column):

| Scenario | Passed | Failed |
|---|---|---|
| `server-stateless` | 21 | 4 |
| `completion-complete` | 1 | 1 |
| `tools-list` | 4 | 0 |
| `tools-call-simple-text` | 1 | 1 |
| `tools-call-image` | 1 | 1 |
| `tools-call-audio` | 1 | 1 |
| `tools-call-embedded-resource` | 1 | 1 |
| `tools-call-mixed-content` | 1 | 1 |
| `tools-call-error` | 1 | 1 |
| `tools-call-with-progress` | 1 | 1 |
| `server-sse-multiple-streams` | 1 | 0 |
| `resources-list` | 1 | 1 |
| `resources-read-text` | 1 | 1 |
| `resources-read-binary` | 1 | 1 |
| `resources-templates-read` | 1 | 1 |
| `sep-2164-resource-not-found` | 2 | 0 (2 warnings) |
| `prompts-list` | 1 | 1 |
| `prompts-get-simple` | 1 | 1 |
| `prompts-get-with-args` | 1 | 1 |
| `prompts-get-embedded-resource` | 1 | 1 |
| `prompts-get-with-image` | 1 | 1 |
| `dns-rebinding-protection` | 1 | 1 |
| `caching` | 4 | 3 |
| `input-required-result-basic-elicitation` | 1 | 1 |
| `input-required-result-basic-sampling` | 1 | 1 |
| `input-required-result-basic-list-roots` | 1 | 1 |
| `input-required-result-request-state` | 1 | 1 |
| `input-required-result-multiple-input-requests` | 1 | 1 |
| `input-required-result-multi-round` | 1 | 1 |
| `input-required-result-missing-input-response` | 1 | 0 (1 warning) |
| `input-required-result-non-tool-request` | 1 | 1 |
| `input-required-result-result-type` | 1 | 1 |
| `input-required-result-unsupported-methods` | 2 | 0 |
| `input-required-result-tampered-state` | 1 | 1 |
| `input-required-result-capability-check` | 1 | 1 |
| `input-required-result-ignore-extra-params` | 1 | 0 (1 warning) |
| `input-required-result-validate-input` | 3 | 0 |
| `tasks-lifecycle` (not scored) | 1 | 8 |
| `tasks-capability-negotiation` (not scored) | 1 | 4 |
| `tasks-wire-fields` (not scored) | 1 | 3 |
| `tasks-request-state-removal` (not scored) | 1 | 1 |
| `tasks-mrtr-input` (not scored) | 1 | 3 |
| `tasks-request-headers` (not scored) | 2 | 3 |
| `tasks-dispatch-and-envelope` (not scored) | 3 | 6 |
| `tasks-status-notifications` (not scored) | 0 | 0 |
| `tasks-required-task-error` (not scored) | 1 | 1 |
| `tasks-mrtr-composition` (not scored) | 1 | 1 |
| `json-schema-2020-12` (not scored) | 1 | 1 |
| `http-header-validation` (not scored) | 14 | 0 |
| `http-custom-header-server-validation` (not scored) | 1 | 5 |

The passing check in most failing scenarios is the suite's wire-schema check: every message SCF
sent validated against the `2026-07-28` JSON schema.

## The baseline

Every failure in the recorded run falls in one of four groups. The baseline names each scenario
or check and gives the reason on the same line.

1. **The suite's fixture tools.** Many scenarios call tools such as `test_simple_text` or
   `test_missing_capability` that a server built for the suite exposes. SCF exposes only its own
   tools (spec I section 5), so the call fails with "unknown tool". These are entries for
   `server-stateless` (4 checks), the seven `tools-call-*` scenarios and `json-schema-2020-12`.
2. **Features this change leaves out** (issue #210 non-goals): resources, prompts and
   completions (`resources-*`, `prompts-*`, `completion-complete`, `sep-2164-resource-not-found`,
   three `caching` checks), multi round-trip input requests (`input-required-result-*`) and the
   Tasks extension (`tasks-*`).
3. **Custom request headers.** No SCF tool marks a parameter with `x-mcp-header`, so
   `http-custom-header-server-validation` has nothing to test.
4. **SCF policy.** `dns-rebinding-protection` expects a localhost `Origin` to be accepted. SCF
   accepts a browser `Origin` only when `FORGE_MCP_ALLOWED_ORIGINS` lists it, and lists none by
   default (spec I section 4), so the check gets HTTP 403.

Entries name single checks wherever a scenario also exercises something SCF does implement, so
the rest of that scenario still has to pass. Whole scenarios are baselined only for the Tasks
extension and `http-custom-header-server-validation`.

## Known differences not covered by the suite run

- An argument that fails a tool's input model is a JSON-RPC `-32602` error, as before. The
  `2026-07-28` tools page recommends an `isError: true` result for input validation errors; this
  change keeps the existing behaviour for both eras.
- A body that is not JSON gets HTTP 200 with `-32700`, as before; the era is not known until the
  body parses.
