# Release Readiness

- **Target bar:** Production
- **Overall verdict:** ❌ **NOT MET**
- **Generated (UTC):** 2026-07-04T13:36:10Z
- **Commit:** `8e67f8a7f7091dadae71e5f240aec73e0103e031`
- **Version (cz):** `0.1.0`

> A bar is **MET** only when every gate at-or-below it is `GREEN` or `MANUAL_ATTESTED`. `SKIPPED_NO_CREDS`, `MISSING_EVIDENCE`, `STALE`, `MANUAL_PENDING`, and `RED` all mean **NOT MET** — the engine never infers a pass.

## Beta gates (included)

| Gate | Blocker | Workstream | Status | Evidence (cmd/artifact) | Last-checked |
|---|---|---|---|---|---|
| `G-DB` | #6 | HARD-01 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m postgres -q packages/db | 2026-07-04T13:36:10Z |
| `G-MODEL` | #1 | HARD-02 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q apps/api -k model_provider | 2026-07-04T13:36:10Z |
| `G-RAG-REAL` | #2 | HARD-04 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m realeval -q | 2026-07-04T13:36:10Z |
| `G-GH` | #1 | HARD-05 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k github_app | 2026-07-04T13:36:10Z |
| `G-MCP` | #1 | HARD-06 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k mcp_live | 2026-07-04T13:36:10Z |
| `G-SLACK` | #1 | HARD-07 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k slack_live | 2026-07-04T13:36:10Z |
| `G-BUILD` | #3 | HARD-08 | 🟢 GREEN | deploy/build-manifest.json | 2026-07-04T13:36:10Z |
| `G-TYPES` | #6 | HARD-12 | 🔴 RED | make typecheck | 2026-07-04T13:36:10Z |
| `G-SEC-AUTOMATED` | #4 | HARD-09 | 🟢 GREEN | uv run pytest -m security -q | 2026-07-04T13:36:10Z |
| `G-CRYPTO` | #5 | HARD-10 | 🟢 GREEN | uv run pytest -q apps/api/tests/test_auth_crypto_envelope.py apps/api/tests/test_cli_secrets.py | 2026-07-04T13:36:10Z |

## Production gates (included)

| Gate | Blocker | Workstream | Status | Evidence (cmd/artifact) | Last-checked |
|---|---|---|---|---|---|
| `G-IMG-PINNED` | #3 | HARD-08 | 🟢 GREEN | deploy/build-manifest.json | 2026-07-04T13:36:10Z |
| `G-PARKED-CLOSED` | #5 | HARD-11 | ⚪ MISSING_EVIDENCE | release/evidence/parked-closed.md | 2026-07-04T13:36:10Z |
| `G-PERF` | #6 | HARD-13 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m perf -q packages/evaluation | 2026-07-04T13:36:10Z |
| `G-MIGRATE` | #6 | HARD-13 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m postgres -q -k migrat | 2026-07-04T13:36:10Z |
| `G-SOAK` | #6 | HARD-13 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m soak -q packages/evaluation | 2026-07-04T13:36:10Z |
| `G-COVERAGE` | #6 | HARD-12 | ⚪ MISSING_EVIDENCE | release/evidence/coverage.json | 2026-07-04T13:36:10Z |
| `G-SEC-EVIDENCE` | #4 | HARD-09 | 🟢 GREEN | release/sbom/forge-source.cdx.json, docs/self-hosting/security.md, docs/security/pentest-punch-list.md | 2026-07-04T13:36:10Z |
| `G-FWD-COMPAT` | #7 | HARD-14 | 🟢 GREEN | pyproject.toml | 2026-07-04T13:36:10Z |
| `G-PENTEST` | #4 | external | 🟡 MANUAL_PENDING | release/attestations/pentest.yaml | 2026-07-04T13:36:10Z |
| `G-SOAK-FLEET` | #6 | external | 🟡 MANUAL_PENDING | release/attestations/fleet-soak.yaml | 2026-07-04T13:36:10Z |

## Verdict

The **Production** bar is **NOT MET**: 14/20 selected gates are not satisfied.

- `G-DB` — SKIPPED_NO_CREDS: required env not set: FORGE_TEST_DATABASE_URL (command not run)
- `G-MODEL` — SKIPPED_NO_CREDS: required env not set: ANTHROPIC_API_KEY (command not run)
- `G-RAG-REAL` — SKIPPED_NO_CREDS: required env not set: FORGE_RUN_REALEVAL (command not run)
- `G-GH` — SKIPPED_NO_CREDS: required env not set: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (command not run)
- `G-MCP` — SKIPPED_NO_CREDS: required env not set: FORGE_MCP_INTEGRATION_URL (command not run)
- `G-SLACK` — SKIPPED_NO_CREDS: required env not set: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET (command not run)
- `G-TYPES` — RED: exit 2: …"AutomationRule" has no attribute "status"  [attr-defined] apps/worker/forge_worker/tasks/automations.py:328: error: Argument 1 has incompatible type "AutomationRule"; expected "AutomationExecution"  [arg-type] apps/worker/forge_worker/tasks/approvals.py:106: error: "Result[Any]" has no attribute "rowcount"  [attr-defined] apps/api/forge_api/cli.py:37: error: Argument "workflow_execution_retention_period" to "RegisterNamespaceRequest" has incompatible type "timedelta"; expected "Duration \| None"  [arg-type] apps/api/forge_api/cli.py:85: error: Function is missing a return type annotation  [no-untyped-def] apps/api/forge_api/cli.py:141: error: Function is missing a return type annotation  [no-untyped-def] Found 115 errors in 47 files (checked 439 source files)  make: *** [typecheck] Error 1
- `G-PARKED-CLOSED` — MISSING_EVIDENCE: artifact missing: release/evidence/parked-closed.md
- `G-PERF` — SKIPPED_NO_CREDS: required env not set: FORGE_RUN_PERF (command not run)
- `G-MIGRATE` — SKIPPED_NO_CREDS: required env not set: FORGE_TEST_DATABASE_URL (command not run)
- `G-SOAK` — SKIPPED_NO_CREDS: required env not set: FORGE_RUN_SOAK (command not run)
- `G-COVERAGE` — MISSING_EVIDENCE: artifact missing: release/evidence/coverage.json
- `G-PENTEST` — MANUAL_PENDING: awaiting a signed human attestation
- `G-SOAK-FLEET` — MANUAL_PENDING: awaiting a signed human attestation

## Honest asterisk (verbatim)

> Code- and evidence-ready for production, pending an external human penetration test and a real multi-week multi-tenant fleet soak — neither performable by the build agents; both are named, scoped, and handed off.
>
> Compliance attestation (SOC2 etc.) is out of scope.

