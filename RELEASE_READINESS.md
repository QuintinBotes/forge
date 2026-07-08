# Release Readiness

- **Target bar:** Beta
- **Overall verdict:** ❌ **NOT MET**
- **Generated (UTC):** 2026-07-08T06:39:16Z
- **Commit:** `3ac3a6c8bfd36654eb12aa16f02fbe5b0d240a02`
- **Version (cz):** `0.1.0`

> A bar is **MET** only when every gate at-or-below it is `GREEN` or `MANUAL_ATTESTED`. `SKIPPED_NO_CREDS`, `MISSING_EVIDENCE`, `STALE`, `MANUAL_PENDING`, and `RED` all mean **NOT MET** — the engine never infers a pass.

## Beta gates (included)

| Gate | Blocker | Workstream | Status | Evidence (cmd/artifact) | Last-checked |
|---|---|---|---|---|---|
| `G-DB` | #6 | HARD-01 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m postgres -q packages/db | 2026-07-08T06:39:16Z |
| `G-MODEL` | #1 | HARD-02 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q apps/api -k model_provider | 2026-07-08T06:39:16Z |
| `G-RAG-REAL` | #2 | HARD-04 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m realeval -q | 2026-07-08T06:39:16Z |
| `G-GH` | #1 | HARD-05 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k github_app | 2026-07-08T06:39:16Z |
| `G-MCP` | #1 | HARD-06 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k mcp_live | 2026-07-08T06:39:16Z |
| `G-SLACK` | #1 | HARD-07 | ⏭️ SKIPPED_NO_CREDS | uv run pytest -m integration -q -k slack_live | 2026-07-08T06:39:16Z |
| `G-BUILD` | #3 | HARD-08 | 🟢 GREEN | deploy/build-manifest.json | 2026-07-08T06:39:16Z |
| `G-TYPES` | #6 | HARD-12 | 🟢 GREEN | make typecheck | 2026-07-08T06:39:16Z |
| `G-SEC-AUTOMATED` | #4 | HARD-09 | 🟢 GREEN | uv run pytest -m security -q | 2026-07-08T06:39:16Z |
| `G-CRYPTO` | #5 | HARD-10 | 🟢 GREEN | uv run pytest -q apps/api/tests/test_auth_crypto_envelope.py apps/api/tests/test_cli_secrets.py | 2026-07-08T06:39:16Z |

## Verdict

The **Beta** bar is **NOT MET**: 6/10 selected gates are not satisfied.

- `G-DB` — SKIPPED_NO_CREDS: required env not set: FORGE_TEST_DATABASE_URL (command not run)
- `G-MODEL` — SKIPPED_NO_CREDS: required env not set: ANTHROPIC_API_KEY (command not run)
- `G-RAG-REAL` — SKIPPED_NO_CREDS: required env not set: FORGE_RUN_REALEVAL (command not run)
- `G-GH` — SKIPPED_NO_CREDS: required env not set: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (command not run)
- `G-MCP` — SKIPPED_NO_CREDS: required env not set: FORGE_MCP_INTEGRATION_URL (command not run)
- `G-SLACK` — SKIPPED_NO_CREDS: required env not set: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET (command not run)

## Honest asterisk (verbatim)

> Code- and evidence-ready for production, pending an external human penetration test and a real multi-week multi-tenant fleet soak — neither performable by the build agents; both are named, scoped, and handed off.
>
> Compliance attestation (SOC2 etc.) is out of scope.

