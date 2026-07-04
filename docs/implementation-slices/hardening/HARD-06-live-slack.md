# HARD-06 — Live Slack Integration (bot token + signed slash command + approval interactivity)

> Phase: hardening · Blocker(s): #1 (no real external systems exercised) · Status target: **DONE = the `SlackNotifier` posts a real approval/notification message to a real Slack workspace via the bot token (asserted on `ok:true` + `ts`), an inbound `/forge` slash-command request passes `X-Slack-Signature` v0 verification (and a bad/stale request is rejected), and an interactive Approve/Reject payload round-trips into the approval store — all behind `@pytest.mark.integration`, creds-gated and skip-clean.** This is the SPEC's **G-SLACK** gate (the SPEC labels the Slack workstream HARD-07; this program ships it as the slice file `HARD-06-live-slack` — same gate, same blocker). "Verified" requires real creds (`SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` for a disposable test workspace); the hermetic default suite stays green and network-free without them.

---

## 1. Intent — what & why

MORNING_REPORT §1.13 and §6 are blunt: the integration SDK's Slack support is "DONE (fixtures only)" — `SlackNotifier` is exercised exclusively through `httpx.MockTransport`, and "No interaction with a real external system has ever happened." The formatter (`build_approval_message`) and the inbound verifier (`verify_slack_signature`) are unit-correct against hand-built payloads, but no message has ever reached `slack.com/api`, and no real Slack-signed request has ever hit the API. That is exactly release blocker #1.

Slack is the human-in-the-loop surface for Forge's **Human Approval System** (`docs/FORGE_SPEC.md` → "Human Approval System" / "Approval UI Must Show"). When a workflow/agent raises an approval gate, a reviewer must (a) *receive* a Block Kit message with the gate context (title, summary, changed-file count, verification pass/fail, confidence, risks) and (b) be able to *act* on it — Approve / Reject / Request changes — without leaving Slack, plus drive ad-hoc operations via the `/forge` slash command. Every inbound path from Slack is an **untrusted intake** authenticated only by Slack's `v0` request signature; getting that boundary right is a security requirement, not a nicety.

HARD-06 turns the three Slack directions from "compiles against fixtures" into "exercised against a real workspace", **without changing the product surface or the frozen contracts**:

1. **Outbound notify** — `SlackNotifier.notify` / `notify_approval` post real `chat.postMessage` calls with the bot token, with bounded retries that honour Slack's `429 Retry-After` and `5xx` backoff, and verified secret redaction on the request/response path.
2. **Inbound slash command** — a new signature-verified `POST /integration/slack/commands` route accepts `/forge ...` invocations (`application/x-www-form-urlencoded`), verifies `X-Slack-Signature` + timestamp anti-replay, and returns an ephemeral Block Kit response within Slack's 3-second budget.
3. **Inbound interactivity** — a new signature-verified `POST /integration/slack/interactions` route accepts the Block Kit `block_actions` payload (`payload=<json>` form field) from the Approve/Reject/Request-changes buttons `build_approval_message` already emits, resolves the embedded approval id, and round-trips a decision into the existing `ApprovalStore`.

This **extends** `packages/integration-sdk` (`forge_integrations.slack`, `forge_integrations.webhooks`) and `apps/api` (`forge_api.routers.integration`, reusing `forge_api.routers.approval.ApprovalStore`). No new package; no contract change — `SlackMessage`, `SlackDeliveryResult`, `ApprovalRequest`, `ApprovalGate`, `ApprovalStatus`, and the `SlackNotifier` Protocol are all frozen and reused verbatim.

## 2. User-facing / operator behavior

- **Journey A — Reviewer receives an approval in Slack.** A task hits a PR gate. The workflow layer calls `notify_approval(ApprovalRequest)`; a Block Kit message lands in the configured channel showing `*Approval needed: pr*`, the title, changed-file count, "Verification: 2/3 checks passed", confidence, and risks, with three buttons (Approve / Reject / Request changes). The reviewer sees a real message (not a fixture) and the API records `SlackDeliveryResult(ok=true, channel=…, ts=…)`.
- **Journey B — Reviewer clicks Approve in Slack.** Slack POSTs a signed `block_actions` interactive payload to `POST /integration/slack/interactions`. The API verifies the `v0` signature, parses the embedded `approval:{id}` / `approve:{id}` value, maps the Slack actor to a decider identity, and calls `ApprovalStore.decide(..., status=approved)`. Slack receives a 200 with an updated message ("Approved by @reviewer") within 3 s; the gate is now `approved` in Forge.
- **Journey C — Operator runs `/forge`.** A developer types `/forge status TASK-123` in Slack. Slack POSTs a signed `application/x-www-form-urlencoded` body to `POST /integration/slack/commands`. The API verifies the signature + timestamp, dispatches the sub-command, and returns an ephemeral Block Kit response. An unrecognised sub-command returns a help block; a malformed/expired/unsigned request is rejected with 401 and no side effect.
- **Journey D — Operator misconfiguration / hostile input.** If `SLACK_SIGNING_SECRET` is unset, both inbound routes fail **closed** with `501 Not Configured` (mirrors the F17 alerts-webhook pattern in `routers/alerts.py`) — no request is ever trusted by default. A replayed request older than 300 s, or one with a forged signature, returns 401. No bot token, signing secret, or request body ever appears in logs, traces, or audit rows.
- **Operator behavior — retries & rate limits.** When Slack returns `429`, the notifier waits `Retry-After` seconds (bounded, max attempts = `slack_max_retries`, default 3) and retries; on `5xx` it backs off exponentially; on terminal failure it returns `SlackDeliveryResult(ok=false, error=…)` rather than raising into the caller — approval delivery failure degrades gracefully (the gate still exists in the API and the web Approval UI).

## 3. Vertical slice

### 3.1 Data model

**No new tables and no migration.** Approval state already lives in `forge_api.routers.approval.ApprovalStore` (in-memory today; DB-backed store swaps in behind the same dependency per the existing module docstring). HARD-06 only *reads/decides* existing `ApprovalRequest` rows and *sends* messages — it adds no columns.

Two small, additive concerns that ride existing structures (no schema change):

- **Slack message-ts back-reference (optional, behind existing `payload`).** When `notify_approval` succeeds, the returned `ts` + resolved `channel` are stashed into the in-memory store alongside the approval (a `dict[uuid.UUID, tuple[str, str]]` keyed by approval id, exactly as `ApprovalStore._owner` already tracks workspace ownership) so the interactivity handler can post a threaded/updated message. `ApprovalRequest` is a **frozen contract** (no `slack_ts` field) — ownership-adjacent state is tracked beside the item, never by mutating the DTO, matching the existing `ApprovalStore` pattern.
- **Audit rows** are written through the existing `forge_api.observability.audit` writer (append-only), not a new table.

### 3.2 Backend

**`apps/api/forge_api/routers/integration.py` (extend).** Add three things alongside the existing `/integration/slack/notify`:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/integration/slack/notify` | RBAC WRITE (unchanged) | Existing outbound notify (now with retry-aware client). |
| `POST` | `/integration/slack/approvals/{approval_id}/notify` | RBAC WRITE | Post the Block Kit approval message for an existing gate; records `ts`. |
| `POST` | `/integration/slack/commands` | **Slack v0 signature** (no principal dep) | `/forge` slash command intake. |
| `POST` | `/integration/slack/interactions` | **Slack v0 signature** (no principal dep) | Block Kit `block_actions` intake → approval decision. |

The two inbound routes mirror the established untrusted-intake pattern in `routers/alerts.py` and the GitHub-webhook route in this same file: they are **deliberately outside** `get_current_principal`; the Slack signature is the *only* trust boundary. They read the **raw body** (`await request.body()`) before any parsing, verify with `verify_slack_signature`, and fail closed (501) when the secret is unconfigured, 401 on bad/missing/stale signature.

New DI dependencies (overridable in tests, same shape as `get_slack_notifier` / `get_github_webhook_secret`):

```python
def get_slack_signing_secret() -> str | None:
    return get_settings().slack_signing_secret

SlackSigningSecretDep = Annotated[str | None, Depends(get_slack_signing_secret)]
```

The inbound handlers reuse `forge_api.routers.approval.get_approval_store` (imported, not duplicated) to apply decisions, and the existing `forge_api.observability.audit` writer + `redact_*` for the audit trail.

**`packages/integration-sdk/forge_integrations/slack.py` (extend `SlackNotifier`).** Keep the frozen Protocol surface (`notify`, `notify_approval`, `health`) and the injectable `transport` (so unit tests keep using `httpx.MockTransport`). Add:

- Retry/backoff around `chat.postMessage` and `auth.test`: a private `_post(path, payload)` that, on `429`, sleeps `min(Retry-After, cap)` and retries up to `max_retries`; on `5xx`, exponential backoff (`base * 2**attempt`, jittered, capped); on `2xx` with `{"ok": false, "error": "rate_limited"}` treats it like 429. Terminal failure returns `SlackDeliveryResult(ok=false, error=…)` — never raises (matches existing `notify` behavior on `httpx.HTTPError`).
- Constructor gains `max_retries: int = 3`, `retry_base_delay: float = 0.5`, `retry_cap_seconds: float = 30.0`, and an injectable `sleep: Callable[[float], None] = time.sleep` so retry timing is deterministic and instantaneous in tests.
- `update_message(channel, ts, blocks, text)` → `SlackDeliveryResult` calling `chat.update` (used by the interactivity handler to render "Approved by …" in-place). Same retry path.

**`packages/integration-sdk/forge_integrations/webhooks.py` (extend).** `verify_slack_signature` already exists and is correct (`v0:{ts}:{body}` HMAC-SHA256, 300 s anti-replay, constant-time compare). Add two small helpers next to it:

```python
def sign_slack_payload(secret: str, timestamp: str, body: bytes | str) -> str:
    """Compute the X-Slack-Signature v0 header for tests/clients."""

def parse_slack_interaction(form_payload: str) -> dict[str, Any]:
    """Parse the Block Kit `payload=<urlencoded-json>` form field into a dict
    (raises on malformed JSON; no network)."""
```

`sign_slack_payload` makes the unit + integration tests able to build correctly-signed requests without hand-rolling HMAC, and documents the exact signing the verifier expects.

### 3.3 Worker/agent

No new Celery task. The workflow/agent layer that *raises* a gate already creates an `ApprovalRequest` (F08 flow); HARD-06 adds an optional, best-effort outbound notify call at gate-creation time (in the existing approval-create path / the worker step that publishes a gate), guarded so a Slack outage never blocks the workflow FSM — a failed `SlackDeliveryResult(ok=false)` is logged (redacted) and the gate proceeds to live in the API/web UI regardless. No LangGraph change.

### 3.4 Frontend

None required for the gate. The web Approval UI (`apps/web`) is unchanged — Slack is an *additional* channel for the same `ApprovalStore`, so a decision made in Slack is visible in the web UI and vice-versa (single source of truth). Optional follow-up (out of scope, §12): a workspace settings toggle for the Slack channel and a "notified in Slack" badge on the approval card.

### 3.5 Infra/deploy/CI

- **Env wiring.** `deploy/.env.example` + `deploy/.env.production.example` gain commented `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_DEFAULT_CHANNEL` (the first two are secrets — values never committed). The `apps/api` service in `deploy/docker-compose.yml` reads them from the environment.
- **Integration creds.** Real values live only in the gitignored `.env.integration` (repo root); `.env.integration.example` (names only) is committed. `.gitignore` already covers `.env`, `.env.*` (except `*.example`), `*.pem` — verified at `/Users/quintinbotes/Projects/forge/.gitignore` lines 26–31.
- **CI.** A new opt-in workflow lane `slack-integration` runs `uv run pytest -m integration -k slack` **only** when the `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` repo secrets are present (guarded by `if: ${{ secrets.SLACK_BOT_TOKEN != '' }}`); the default `test` lane runs `-m "not integration"` and stays network-free. The whole-suite green gate is unchanged for the hermetic lane.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**Frozen contracts reused as-is (no change):** `forge_contracts.SlackMessage(channel, text, blocks, thread_ts)`, `SlackDeliveryResult(ok, channel, ts, error)`, `ApprovalRequest(...)`, `ApprovalGate`, `ApprovalStatus`, and the `SlackNotifier` Protocol (`notify`, `notify_approval`, `health`).

**`forge_integrations.slack.SlackNotifier` (extended constructor + methods):**

```python
class SlackNotifier:
    def __init__(
        self,
        *,
        token: str | None = None,
        default_channel: str | None = None,
        base_url: str = DEFAULT_BASE_URL,            # "https://slack.com/api"
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,                        # NEW
        retry_base_delay: float = 0.5,               # NEW (seconds)
        retry_cap_seconds: float = 30.0,             # NEW
        sleep: Callable[[float], None] = time.sleep, # NEW (injectable for tests)
    ) -> None: ...

    def notify(self, message: SlackMessage) -> SlackDeliveryResult: ...          # retry-aware
    def notify_approval(self, request: ApprovalRequest) -> SlackDeliveryResult: ...
    def update_message(                                                          # NEW
        self, *, channel: str, ts: str,
        text: str, blocks: list[dict[str, Any]] | None = None,
    ) -> SlackDeliveryResult: ...
    def health(self) -> HealthResult: ...
    def build_approval_message(self, request: ApprovalRequest,
                               channel: str | None = None) -> SlackMessage: ...  # unchanged
```

**`forge_integrations.webhooks` (existing + new):**

```python
def verify_slack_signature(secret: str, timestamp: str, body: bytes | str,
                           signature: str | None, *,
                           max_skew_seconds: int = 300,
                           now: float | None = None) -> bool: ...   # EXISTING (reused)
def sign_slack_payload(secret: str, timestamp: str, body: bytes | str) -> str: ...  # NEW
def parse_slack_interaction(form_payload: str) -> dict[str, Any]: ...               # NEW
```

**FastAPI routes (`apps/api/forge_api/routers/integration.py`):**

```python
# Inbound — Slack v0 signature is the ONLY trust boundary (no principal dep).
@router.post("/slack/commands")
async def slack_slash_command(
    request: Request,
    secret: SlackSigningSecretDep,
    store: ApprovalStoreDep,
    x_slack_signature: Annotated[str | None, Header()] = None,
    x_slack_request_timestamp: Annotated[str | None, Header()] = None,
) -> JSONResponse: ...

@router.post("/slack/interactions")
async def slack_interaction(
    request: Request,
    secret: SlackSigningSecretDep,
    store: ApprovalStoreDep,
    x_slack_signature: Annotated[str | None, Header()] = None,
    x_slack_request_timestamp: Annotated[str | None, Header()] = None,
) -> JSONResponse: ...

# Outbound — RBAC WRITE (gated principal), reuses approval store.
@router.post("/slack/approvals/{approval_id}/notify",
             response_model=SlackDeliveryResult, dependencies=[WriteGate])
def slack_notify_approval(notifier: SlackDep, store: ApprovalStoreDep,
                          principal: WriterDep, approval_id: uuid.UUID) -> SlackDeliveryResult: ...
```

**New DI deps:** `get_slack_signing_secret() -> str | None`; `ApprovalStoreDep` imported from `forge_api.routers.approval.get_approval_store`.

**Settings (`apps/api/forge_api/settings.py`) — new keys (env-overridable, secrets default `None`):**

```python
slack_signing_secret: str | None = None      # SLACK_SIGNING_SECRET (v0 verify; 501 if unset)
slack_max_retries: int = 3                    # SLACK_MAX_RETRIES
slack_retry_base_delay_seconds: float = 0.5   # SLACK_RETRY_BASE_DELAY_SECONDS
slack_signature_max_skew_seconds: int = 300   # SLACK_SIGNATURE_MAX_SKEW_SECONDS
```

Existing keys reused: `slack_token` (`SLACK_BOT_TOKEN`), `slack_default_channel` (`SLACK_DEFAULT_CHANNEL`).

**Env vars (names only; values live in gitignored `.env.integration`):**
`SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_SIGNING_SECRET`, `SLACK_DEFAULT_CHANNEL`, optional `SLACK_TEST_CHANNEL` (integration target), `SLACK_MAX_RETRIES`, `SLACK_RETRY_BASE_DELAY_SECONDS`, `SLACK_SIGNATURE_MAX_SKEW_SECONDS`. BYOK alternative: the per-workspace token may instead be resolved from the encrypted vault (`forge_api.auth.vault.SecretVault`) under `APIKeyKind.INTEGRATION_TOKEN` at call time and discarded — never held in a module global.

## 5. Dependencies (other slices/foundation that must exist first)

- **Foundation (exists):** `apps/api` skeleton + `forge_api.settings` + `forge_api.deps` (principal/RBAC) + `forge_api.routers._rbac.require_permission`; `forge_api.observability` (audit writer + `redact_text`/`redact_value`/`redact_mapping`, `REDACTED="[REDACTED]"`); `forge_contracts` (frozen `SlackMessage`/`SlackDeliveryResult`/`ApprovalRequest`/enums + `SlackNotifier` Protocol).
- **`forge_integrations.slack` + `forge_integrations.webhooks` (exists):** `SlackNotifier`, `verify_slack_signature` — extended here, not replaced.
- **`forge_api.routers.approval` (exists):** `ApprovalStore`, `get_approval_store`, `ApprovalStatus` decision flow — reused by the interactivity handler.
- **HARD-10 (crypto/secret-key/vault) — SOFT.** Required only for the *BYOK-from-vault* token resolution variant; the env-var (`SLACK_BOT_TOKEN`) path needs nothing from HARD-10. Per the SPEC sequencing, when BYOK creds flow through the vault, HARD-10's real cipher/secret-key must be active first.
- **F08 (Plan→Execute→Verify→PR→Approval) — SOFT.** Produces the `ApprovalRequest` gates that get notified; HARD-06 ships its own fixture gates so it is testable independently.
- **No dependency on HARD-01/03/05/07** — Slack is independent of DB-substrate, embedder, GitHub, and MCP workstreams (parallelizable per the SPEC's "four real-external integrations, parallelizable" note).

## 6. Acceptance criteria (numbered, testable)

Marked **[offline]** (runs in the hermetic default suite, no creds) or **[creds]** (`@pytest.mark.integration`, skips clean when creds absent).

1. **[creds]** `SlackNotifier.notify` posts a real `chat.postMessage` to `SLACK_TEST_CHANNEL` using `SLACK_BOT_TOKEN` and returns `SlackDeliveryResult(ok=true, channel=…, ts=<non-empty>)`. (G-SLACK headline AC #1.)
2. **[creds]** `notify_approval(ApprovalRequest)` posts the Block Kit approval message (Approve/Reject/Request-changes buttons present, approval id embedded) and returns `ok:true` + `ts`.
3. **[offline]** `verify_slack_signature` accepts a correctly `v0`-signed request (built via `sign_slack_payload`) and **rejects** (a) a wrong-secret signature, (b) a tampered body, (c) a missing signature, and (d) a timestamp older than `max_skew_seconds` (stale → replay defense). (G-SLACK headline AC #2.)
4. **[offline]** `POST /integration/slack/commands` returns **501** when `slack_signing_secret` is unset (fail-closed), **401** on bad/missing/stale signature, and **200** with an ephemeral Block Kit body for a valid signed `/forge help`.
5. **[offline]** `POST /integration/slack/interactions` with a valid signed `block_actions` Approve payload (value `approve:{approval_id}`) calls `ApprovalStore.decide` → the gate's status becomes `approved`, `decided_by` reflects the Slack actor identity, and the response is 200; a Reject payload yields `rejected`. (G-SLACK headline AC #3 — interactive round-trip.)
6. **[offline]** A `block_actions` payload whose embedded approval id does not exist (or belongs to another workspace) returns 200 to Slack (so Slack does not retry) but performs **no** state change and writes an audit row noting the no-op.
7. **[offline]** Retry path: a `429` with `Retry-After: 1` followed by a `200` (driven via `MockTransport` + injected no-op `sleep`) results in `ok:true` and exactly 2 attempts; `max_retries` exhausted on persistent `429`/`5xx` returns `SlackDeliveryResult(ok=false, error=…)` without raising.
8. **[offline]** Redaction: `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, the `Authorization: Bearer …` header, and any signature value never appear in any log line, audit row, trace, or error message produced by the notify/command/interaction paths (asserted against captured logs + audit rows through the existing `redact_*` filter).
9. **[offline]** `update_message` issues `chat.update` with the supplied `ts`/`channel` and is exercised by the interactivity handler to render an "Approved by …" in-place update (asserted via `MockTransport` request capture).
10. **[offline]** The frozen contracts are unchanged: a contract-conformance test confirms `SlackNotifier` still satisfies the `forge_contracts.SlackNotifier` Protocol and `SlackMessage`/`SlackDeliveryResult` fields are untouched.
11. **[creds]** Inbound real-request smoke: a request captured from Slack's "Verify URL"/test-invocation (or replayed with a freshly computed real signature against the real signing secret) passes verification end-to-end through the route.
12. **[offline]** Whole-suite green gate holds: `uv run pytest -q` (with `-m "not integration"`), `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck` exit 0, `cd apps/web && pnpm test` — no regression; integration tests skip cleanly when creds are absent.

## 7. Test plan (TDD) — unit + integration (gated on env creds) + how to run

**TDD discipline:** write the failing test first (per `superpowers:test-driven-development`); integration-sdk min coverage floor per its profile. Tests live in `packages/integration-sdk/tests/test_slack.py` (extend), `packages/integration-sdk/tests/test_webhooks.py` (extend), and `apps/api/tests/test_integration_router.py` (extend) + a new `apps/api/tests/test_slack_inbound.py`.

**Fixtures / helpers:**
- `make_transport(handler)` + `RequestRecorder` — existing in `packages/integration-sdk/tests/conftest.py`; reused to assert outbound request shape with zero network.
- `signed_slack_request(secret, body, *, skew=0)` — new test helper wrapping `sign_slack_payload` to build `(headers, body)` for inbound route tests.
- `authenticate_app` — existing `apps/api` fixture for the RBAC-gated outbound route.

**Unit tests [offline, hermetic] — `packages/integration-sdk/tests/`:**
- `test_notify_retries_on_429_then_succeeds` / `test_notify_gives_up_after_max_retries_returns_ok_false` (AC7) — `MockTransport` returns `429`/`5xx`, injected `sleep=lambda _: None`, assert attempt count + result.
- `test_notify_respects_retry_after_header` — assert the value passed to `sleep` equals `Retry-After` capped at `retry_cap_seconds`.
- `test_update_message_calls_chat_update_with_ts` (AC9).
- `test_sign_then_verify_roundtrips` ; `test_verify_rejects_wrong_secret_tampered_missing_and_stale` (AC3).
- `test_parse_slack_interaction_decodes_payload_and_raises_on_garbage`.
- `test_secret_never_in_repr_or_str` (AC8 — `SlackNotifier.__repr__` is secret-safe).
- `test_slacknotifier_satisfies_frozen_protocol` (AC10).

**API tests [offline] — `apps/api/tests/test_slack_inbound.py`:**
- `test_commands_501_when_secret_unset` ; `test_commands_401_on_bad_signature` ; `test_commands_401_on_stale_timestamp` ; `test_commands_200_help_for_valid_signed_request` (AC4).
- `test_interaction_approve_decides_gate` ; `test_interaction_reject_decides_gate` (AC5) — seed `ApprovalStore` via DI override, post signed `payload=…`, assert status transition + `decided_by`.
- `test_interaction_unknown_or_cross_tenant_id_is_noop_200` (AC6).
- `test_no_secret_or_signature_in_logs_or_audit` (AC8) — capture logs/audit, assert `[REDACTED]`.
- Extend existing `test_integration_router.py::test_slack_notify` to also cover `/slack/approvals/{id}/notify` (records `ts`).

**Integration tests [creds] — `packages/integration-sdk/tests/test_slack.py` + `apps/api/tests/test_slack_inbound.py`, all `@pytest.mark.integration`:**
- `test_live_notify_posts_to_test_channel` (AC1) — skips with a clear reason if `SLACK_BOT_TOKEN`/`SLACK_TEST_CHANNEL` absent; asserts `ok:true` + non-empty `ts`; tidies up via `chat.delete` if possible.
- `test_live_notify_approval_block_kit` (AC2).
- `test_live_signed_request_verifies` (AC11) — sign a payload with the real `SLACK_SIGNING_SECRET` and confirm the route accepts it (proves the verifier matches Slack's real algorithm end-to-end).

**How to run:**
```bash
# Hermetic default (no creds, network-free) — must stay green:
uv run pytest -q -m "not integration"
uv run pytest packages/integration-sdk apps/api/tests/test_slack_inbound.py -q

# Live Slack lane (requires .env.integration with real creds):
set -a; source .env.integration; set +a
uv run pytest -m integration -k slack -q

# Whole-suite gate:
uv run ruff check . && uv run ruff format --check . && make typecheck && \
  uv run pytest -q -m "not integration" && (cd apps/web && pnpm test)
```

## 8. Security & policy considerations

- **Signature is the only inbound trust boundary.** `/slack/commands` and `/slack/interactions` are intentionally outside `get_current_principal` (a provider callback can't carry a Forge principal), exactly like the existing GitHub-webhook and F17 alert-webhook routes. They read the **raw body before parsing**, verify the `v0` HMAC with constant-time compare (`hmac.compare_digest`), and **fail closed**: 501 if no signing secret is configured, 401 on missing/invalid/stale signature — no state change on rejection.
- **Replay defense.** `verify_slack_signature` rejects requests whose `X-Slack-Request-Timestamp` is older than `slack_signature_max_skew_seconds` (default 300, per Slack's guidance). Tested for stale + future skew.
- **No secret ever logged / committed / fixtured.** `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, the `Authorization` header, and signature values pass through the shared `redact_*` filter (`forge_api.observability.redaction`, `REDACTED="[REDACTED]"`) before any log/trace/audit write; `SlackNotifier.__repr__` is secret-safe. `.gitignore` already excludes `.env*`/`*.pem`; only `.env.integration.example` (names only) is committed. (SPEC "Credentials & secrets handling" rules 1, 2.)
- **BYOK resolution at call time.** When the per-workspace token comes from the vault, it is resolved per request under `APIKeyKind.INTEGRATION_TOKEN`, used, and discarded — not stored in a module global (SPEC rule 3).
- **Human-in-the-loop integrity.** A Slack-driven decision maps the Slack actor to a decider identity and writes it as `decided_by`; the decision is recorded through the same audited `ApprovalStore` path as the web UI, preserving the FORGE_SPEC "an agent cannot approve its own gate" guarantee (the `agent-runner` role has no path into the approval decision).
- **Cross-tenant isolation.** Interactivity decisions are scoped to the approval's owning workspace via the existing `ApprovalStore._owner` map; a payload referencing another tenant's approval id is a no-op (AC6) — Slack still gets 200 so it doesn't retry, but Forge state is untouched and the no-op is audited.
- **DoS / abuse bounds.** Outbound retries are bounded (`max_retries`, `retry_cap_seconds`) and honour `Retry-After`; inbound routes do minimal work before signature check (cheap HMAC) so an unsigned flood is rejected before any parse/DB work.
- **SSRF note (audit input for HARD-09).** `base_url` defaults to `https://slack.com/api` and is not caller-controlled on the inbound paths; the outbound URL is fixed, removing a server-side-request-forgery vector. Flag this seam for the HARD-09 pentest punch-list (Slack `response_url` callbacks, if added later, are an outbound-URL surface to validate).

## 9. Effort & risk (S/M/L + risks; note anything that CANNOT be done in-sandbox)

**Effort: M.** Outbound retry + `update_message` S; two signature-verified inbound routes + interactivity→decision wiring M; tests (offline + creds-gated) S–M; settings/env/CI wiring S. The verifier (`verify_slack_signature`) and formatter (`build_approval_message`) already exist, which trims the core.

Risks:
- **Slack 3-second response budget.** Slash-command / interaction handlers must respond within 3 s. Mitigation: do only signature-verify + store-decide synchronously and return immediately; defer any slow side-effects (e.g. posting a follow-up) to a background task or `chat.update` via `response_url` later. (Medium)
- **Signing-secret / token mismatch across environments.** A wrong secret silently fails every inbound request as 401. Mitigation: fail-closed 501 when unset, an explicit health/diagnostic, and the `test_live_signed_request_verifies` creds test that proves the secret matches Slack's algorithm end-to-end. (Medium)
- **Rate limiting realism.** Slack tiers/`Retry-After` behavior can only be *fully* characterized against the live API; the offline tests simulate `429`/`Retry-After` via `MockTransport`. (Low-Med)
- **In-memory `ApprovalStore`.** Slack-ts back-reference and decisions are in-memory until the DB-backed store lands (already a known seam in `routers/approval.py`); HARD-06 does not regress this — it reuses the same dependency. (Low)
- **CANNOT be done in the no-network sandbox:** the **[creds]** integration tests (ACs 1, 2, 11) require a real Slack test workspace (bot token + signing secret) and outbound network; they run only on a networked runner / CI lane with the secrets present, and **skip clean** otherwise. A human still has to create the Slack app, install it to a test workspace, set scopes (`chat:write`, `commands`), and point the slash-command + interactivity request URLs at the deployed API — that one-time Slack-app provisioning is an operator step, not an agent step (named, not hidden).

## 10. Key files / paths (exact, in the real monorepo)

- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/forge_integrations/slack.py` — extend `SlackNotifier` (retry/backoff, `update_message`, injectable `sleep`).
- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/forge_integrations/webhooks.py` — add `sign_slack_payload`, `parse_slack_interaction` (next to existing `verify_slack_signature`).
- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/forge_integrations/__init__.py` — export the two new helpers.
- `/Users/quintinbotes/Projects/forge/apps/api/forge_api/routers/integration.py` — add `/slack/commands`, `/slack/interactions`, `/slack/approvals/{approval_id}/notify`, `get_slack_signing_secret`.
- `/Users/quintinbotes/Projects/forge/apps/api/forge_api/routers/approval.py` — reuse `get_approval_store`/`ApprovalStore.decide` (no change; import only).
- `/Users/quintinbotes/Projects/forge/apps/api/forge_api/settings.py` — add `slack_signing_secret`, `slack_max_retries`, `slack_retry_base_delay_seconds`, `slack_signature_max_skew_seconds`.
- `/Users/quintinbotes/Projects/forge/apps/api/forge_api/observability/redaction.py` — reuse `redact_text`/`redact_value`/`redact_mapping` (no change).
- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/tests/test_slack.py` — extend (retry, update, live creds tests).
- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/tests/test_webhooks.py` — extend (sign/verify roundtrip, parse interaction).
- `/Users/quintinbotes/Projects/forge/packages/integration-sdk/tests/conftest.py` — reuse `make_transport`/`RequestRecorder`; add `signed_slack_request` helper.
- `/Users/quintinbotes/Projects/forge/apps/api/tests/test_slack_inbound.py` — NEW (commands + interactions inbound tests).
- `/Users/quintinbotes/Projects/forge/apps/api/tests/test_integration_router.py` — extend (`/slack/approvals/{id}/notify`).
- `/Users/quintinbotes/Projects/forge/deploy/.env.example`, `/Users/quintinbotes/Projects/forge/deploy/.env.production.example` — add Slack keys (commented; no values).
- `/Users/quintinbotes/Projects/forge/.env.integration.example` — NEW (names only): `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_DEFAULT_CHANNEL`, `SLACK_TEST_CHANNEL`.
- `/Users/quintinbotes/Projects/forge/.github/workflows/ci.yml` — add opt-in `slack-integration` lane guarded on secret presence.

## 11. Research references

- Slack — Verifying requests from Slack (`v0` signing: `v0:{timestamp}:{body}` HMAC-SHA256, 5-minute replay window): https://api.slack.com/authentication/verifying-requests-from-slack
- Slack Web API `chat.postMessage`: https://api.slack.com/methods/chat.postMessage · `chat.update`: https://api.slack.com/methods/chat.update · `auth.test`: https://api.slack.com/methods/auth.test
- Slack rate limits (`429` + `Retry-After`, method tiers): https://api.slack.com/apis/rate-limits
- Slack slash commands (`application/x-www-form-urlencoded`, 3-second response, ephemeral responses, `response_url`): https://api.slack.com/interactivity/slash-commands
- Slack interactivity / `block_actions` payload (`payload=<urlencoded-json>`): https://api.slack.com/interactivity/handling#payloads
- Slack Block Kit (sections + actions + buttons): https://api.slack.com/block-kit
- In-repo: `docs/FORGE_SPEC.md` → "Human Approval System" / "Approval UI Must Show" / Security table (secret redaction, auth-required); `docs/MORNING_REPORT.md` §1.13 (fixtures-only), §5(4) (live HTTP parked), §6 (provider/transport realism); SPEC §"HARD-07 — Real Slack integration" (the G-SLACK gate this slice ships) + §"Credentials & secrets handling".
- Existing patterns to mirror: `apps/api/forge_api/routers/alerts.py` (untrusted-intake webhook: raw-body verify, 501 fail-closed, 401 bad signature) and the GitHub-webhook route in `apps/api/forge_api/routers/integration.py`.

## 12. Out of scope / future

- **Slack OAuth app distribution / per-workspace install flow** (multi-workspace `oauth.v2.access` + per-team token storage) — V2; HARD-06 uses a single bot token (env or per-workspace vault). OAuth code-exchange itself is HARD-10's IdP work.
- **`response_url` deferred/async responses & follow-up threading** beyond the immediate `chat.update` — future; HARD-06 responds synchronously within the 3-second budget.
- **Slack Events API** (message/mention events, Socket Mode) — not needed for approvals; future.
- **Rich `/forge` sub-command surface** (full task CRUD, board ops from Slack) — HARD-06 ships signature-verified intake + a `help`/`status` skeleton; the command grammar is a follow-up.
- **Workspace settings UI for Slack channel + "notified in Slack" badge** on the web Approval card — frontend follow-up (§3.4).
- **DB-backed approval store + durable Slack-ts back-reference** — rides the existing `ApprovalStore` DB swap-in (not introduced here).
- **Human Slack-app provisioning** (create app, scopes, request URLs) and a **3rd-party pentest** of the inbound signature/SSRF surface — named operator/external steps; the latter is HARD-09's punch-list item, not performable by the build agents.
</content>
</invoke>
