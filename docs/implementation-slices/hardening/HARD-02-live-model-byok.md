# HARD-02 — Live model provider (BYOK Anthropic/OpenAI)

> Phase: hardening · Blocker(s): #1 (no real external systems exercised), #2 (eval/runtime numbers are offline/deterministic — the agent loop has never run against a learned model) · Status target: **VERIFIED** means a minimal `AgentObjective` runs end-to-end through `forge_agent.AgentRunner` against a *real* BYOK provider chosen by `FORGE_MODEL_PROVIDER` (anthropic|openai), reaching a terminal state with the real `langgraph.StateGraph` driving plan→act→observe; the live request/response path leaks no API key into any log/trace/audit row/step; per-run token+cost are recorded to the observability path. The default hermetic suite stays green and network-free (the live test is `@pytest.mark.integration`, skips cleanly when creds absent — never silently falls back to a fake on the integration lane). **DONE-without-creds** means the provider-agnostic seam, both adapters, the usage/cost accounting, the refusal handling, and redaction are implemented and unit-tested against an injected fake/mock transport; **DONE** (gate G-MODEL) requires the live run, which needs a real BYOK key.

---

## 1. Intent — what & why

The ALPHA agent runtime is real (LangGraph `StateGraph`, policy-gated tool dispatch, worktree sandbox, confidence/handoff) but it has **never executed against a learned model**. `apps/worker/forge_worker/agent_runner.py:build_agent_runner` injects a `ScriptedModelClient` (`forge_agent.testing`), and `forge_contracts.ModelClient` has no production implementation anywhere in the tree — only the deterministic fake. MORNING_REPORT §5(4) names this exactly: *"Live model/reranker/embedding HTTP calls — forbidden overnight. Real-provider path not called."* This is release blocker #1 (no real external system exercised) and the runtime half of blocker #2 (everything model-shaped is deterministic).

HARD-02 supplies the missing piece: a **provider-agnostic BYOK model client** that satisfies the frozen `ModelClient` Protocol, with two real adapters (Anthropic via the official `anthropic` SDK; OpenAI via the official `openai` SDK), wired into the three model consumers named in scope:

1. **agent-runtime** (`forge_agent`) — the plan→act→observe loop calls `ModelClient.complete()` per turn; HARD-02 makes the injected client real, aggregates token/cost usage per run, and handles the real-world `stop_reason == "refusal"` path.
2. **spec-engine** (`forge_spec`) — `spec_clarify` / `spec_plan` gain an *optional* injected `ModelClient` for model-assisted clarification/plan suggestions (today they are template/heuristic; the injection is additive and degrades to the template path when no client is configured).
3. **embeddings** (`forge_knowledge`) — HARD-02 owns the **shared BYOK key resolution + provider config** (vault `APIKeyKind.MODEL_PROVIDER`, env, redaction) that `HttpEmbeddingClient` consumes; the *quality/eval* of a real embedder is HARD-03/04, not here.

The design deliberately keeps the **provider-agnostic seam at the `ModelClient` Protocol** (per the FOUNDATION rule: extend `forge_*`, conform to frozen `forge_contracts`) and uses each provider's **official SDK** behind that seam — not a single OpenAI-compatible HTTP shim — so streaming, retries, token accounting, refusal detection, and tool-call parsing are handled by maintained code. Anthropic is the reference/default provider (`MODEL_PROVIDER=anthropic` in `.env.example`), default model `claude-opus-4-8`.

## 2. User-facing / operator behavior

HARD-02 is internal plumbing, but it has concrete operator-observable behavior:

- **Journey A — Operator supplies a BYOK key, the agent runs for real.** An operator puts `FORGE_MODEL_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=…` in the gitignored `.env.integration` (or stores the key in the per-workspace vault via the existing auth surface, `APIKeyKind.MODEL_PROVIDER`). A queued `forge.agent.run` task now plans/acts/observes against the live model; the run trace shows real model `thought` steps, real tool dispatch, and a terminal `OUTPUT`/`HANDOFF` step. No key value appears anywhere in the trace, audit rows, or worker logs.
- **Journey B — No creds, nothing breaks.** With no provider key configured, `build_agent_runner()` keeps using the offline `ScriptedModelClient`; the worker still runs end-to-end (degraded, deterministic). The integration test that needs a real key **skips with a clear reason**; the default suite stays green and network-free.
- **Journey C — Cost visibility.** After a real run, the operator sees per-run token totals (`input_tokens`/`output_tokens`) and a derived USD cost in `AgentRunResult.artifacts["model_usage"]` and on the observability span/usage record (computed from a small per-model pricing table). Cache reads (when prompt caching is on) lower the reported cost.
- **Journey D — Provider swap.** Changing `FORGE_MODEL_PROVIDER` to `openai` (with `OPENAI_API_KEY` + `FORGE_MODEL_NAME`) routes the same `AgentObjective` through OpenAI with no code change — the factory selects the adapter at build time.
- **Journey E — Refusal / safety stop.** If the provider declines a request (`stop_reason: "refusal"` on Opus 4.8, or an OpenAI content-policy stop), the runner records it as a terminal non-success and escalates to a human (`needs_human=True`) rather than crashing or looping. The refusal category (not content) is recorded; the prompt is not retried blindly.

## 3. Vertical slice

### 3.1 Data model

No new tables and **no migration** — HARD-02 conforms to the existing `forge_db` schema and frozen contracts.

- **BYOK key storage** reuses the existing `api_key` table (`encrypted_secret` column) behind `forge_api.auth.vault.SecretVault`, keyed by `APIKeyKind.MODEL_PROVIDER` (already defined in `forge_contracts.enums.APIKeyKind`). HARD-10 makes the vault cipher production-grade; HARD-02 only *reads* through the existing `SecretVault.get_secret(workspace_id, secret_id)` API.
- **Usage/cost** is recorded through the existing observability path (`apps/api/forge_api/observability/{trace,audit,otel}.py`) and carried in the open `AgentRunResult.artifacts: dict` (frozen DTO, open value type) under key `model_usage`. `forge_contracts.TokenUsage` is frozen and carries only `input_tokens`/`output_tokens`; **cost (USD) and cache-read counts are derived/recorded outside the DTO** — never added to it. If a persisted per-run usage row is later wanted, it lands on the `agent_run` / audit tables under HARD-01, not in this slice.

### 3.2 Backend

The API surface is unchanged. HARD-02 adds the model-client building blocks the API/worker resolve at request time:

- **`apps/api/forge_api/auth/service.py`** gains a thin resolver `resolve_model_client(workspace_id, *, secret_id | None) -> ModelClient` that (a) reads the BYOK key from the vault (or env fallback for the integration lane), (b) builds the provider config, and (c) returns a `forge_agent.providers` client. The key is resolved per call and discarded — never held in a module global, never logged. (This lives next to the existing OAuth/vault facade.)
- **No new routes.** Existing `apps/api/forge_api/routers/agent.py` and `spec.py` already mount the runtime/spec surfaces; they pass the resolved `ModelClient` into the runner/engine via DI rather than constructing a fake.

### 3.3 Worker / agent runtime

This is the heart of the slice. New subpackage **`packages/agent-runtime/forge_agent/providers/`** (extends `forge_agent`; no new top-level package):

```
forge_agent/providers/
├── __init__.py          # public: build_model_client, ModelClientConfig, ProviderName
├── config.py            # ModelClientConfig (env/DI), ProviderName enum, env loader
├── base.py              # factory build_model_client(config, *, redactor) -> ModelClient
├── anthropic_client.py  # AnthropicModelClient  (official `anthropic` SDK)
├── openai_client.py     # OpenAIModelClient     (official `openai` SDK)
├── pricing.py           # MODEL_PRICING table + cost_usd(model, usage, cache_read)
├── usage.py             # UsageAccumulator: per-run token/cost aggregation
└── translate.py         # ModelRequest <-> SDK request/response + tool schema mapping
```

Provider SDKs are **optional, lazily imported extras** (`anthropic`, `openai`): imported *inside* the adapter modules so the hermetic default suite needs neither installed, and an absent SDK on the integration lane raises a clear skip-able `ModelClientUnavailable`. They are NOT added to the base `forge-agent` dependency list — added as an extra (`forge-agent[providers]`) and re-locked under HARD-14.

**`AnthropicModelClient`** (reference impl; implements `ModelClient`):
- Built on the **official Anthropic Python SDK** (`anthropic.Anthropic`), per the claude-api guidance — not raw httpx, not an OpenAI-compatible shim.
- `complete(request)` is implemented **over streaming**: `with client.messages.stream(model=…, max_tokens=…, system=…, messages=…, tools=…, thinking={"type":"adaptive"}, output_config={"effort": cfg.effort}) as s: msg = s.get_final_message()`. Streaming is the default (claude-api: stream anything with high `max_tokens` to avoid the SDK's HTTP-timeout guard) and `get_final_message()` returns the accumulated message without per-event handling.
- Defaults: model `claude-opus-4-8`; **adaptive thinking** (`{"type":"adaptive"}` — `budget_tokens` is rejected with 400 on Opus 4.8, never sent); effort from `cfg.effort` (default `high`); `max_tokens` from `cfg.max_tokens` (default 16000, raised when streaming). No `temperature`/`top_p`/`top_k` (removed on Opus 4.8 — would 400; `ModelRequest.temperature` is dropped for the Anthropic adapter).
- **Tool calls:** Forge tool schemas → Anthropic `tools=[{name, description, input_schema}]`; response `tool_use` content blocks → `forge_contracts.ModelToolCall(id, name, arguments)`; assistant text blocks → `ModelResponse.content`.
- **Refusal:** `msg.stop_reason == "refusal"` → `ModelResponse(stop_reason="refusal", content="", …)` with `stop_details.category` (not content) attached in `ModelResponse` metadata-free path → surfaced via the runner (see below). Guarded *before* reading `content` (claude-api pitfall).
- **Usage:** `msg.usage.input_tokens`/`output_tokens` → `TokenUsage`; `cache_read_input_tokens` (when prompt caching is enabled) is captured for cost only via the accumulator, not the frozen DTO.
- **Prompt caching (cost optimization, optional):** when `cfg.prompt_cache` is true, the stable system prompt is sent with top-level auto-caching (`cache_control={"type":"ephemeral"}`) so the re-sent system prompt across turns reads from cache. Adapter-internal; invisible to the `ModelRequest` contract.
- **Timeouts/retries:** SDK `timeout=cfg.timeout_s` (default 600s) and `max_retries=cfg.max_retries` (default 2 — SDK auto-retries 429/5xx/connection with backoff). Optional `base_url` (`cfg.base_url`) for gateways.

**`OpenAIModelClient`** (implements `ModelClient`):
- Built on the **official OpenAI Python SDK** (`openai.OpenAI`).
- `complete(request)` → `client.chat.completions.create(model=…, messages=…, tools=…, max_tokens=…, stream=True)` accumulated to a final message (streaming default, same timeout rationale). `tool_calls` → `ModelToolCall`; `usage.prompt_tokens`/`completion_tokens` → `TokenUsage`; a content-policy / refusal finish → `stop_reason="refusal"`.
- Defaults from `cfg`: `FORGE_MODEL_NAME` required (no implicit OpenAI model default — operator chooses); `timeout`/`max_retries` via SDK client kwargs.

**`stream(request)`** on both adapters yields `forge_contracts.ModelStreamEvent(type="text", text=…, delta=…)` for real token streaming (consumed by future streaming UIs; the loop uses `complete()`).

**Redaction seam (no cross-package dep on apps/api):** adapters accept an injected `redactor: Callable[[str], str] = lambda s: s`. The worker/API wire `forge_api.observability.redaction.redact_text` as the redactor. Adapters **never log request/response bodies** — they log only metadata (provider, model, latency, token counts, SDK `_request_id`); any exception message is passed through the redactor before being re-raised as `ModelClientError`. This keeps `forge_agent` free of an `apps/api` import while honoring the spec's "single redaction filter is the source of truth" rule (the real `redact_text` catches `Bearer …`, `sk-…`, `ghp_…`, JWTs, AWS keys).

**`AgentRunner` extension** (`packages/agent-runtime/forge_agent/runtime.py`, additive):
- `_call_model` aggregates `response.usage` into a `UsageAccumulator` on the state; `_to_result` writes `artifacts["model_usage"] = {"input_tokens", "output_tokens", "cost_usd", "calls", "cache_read_input_tokens"}`.
- `_call_model`/`_route` handle `response.stop_reason == "refusal"`: set `state.finished`, `state.needs_human=True`, append a `risks` entry (`"model refused: <category>"`), no blind retry → result `RunStatus.ESCALATED`.
- These are extensions to existing methods; the frozen `AgentRuntime.run` signature and `AgentRunResult` shape are unchanged (usage rides the open `artifacts` dict).

**`ToolRegistry.schemas()` extension** (`forge_agent/tools.py`, additive, non-breaking): include an `input_schema` (JSON Schema) per tool so adapters can advertise real tool parameter contracts to the providers. Today `schemas()` returns `{name, description}` only — adding `input_schema` is backward-compatible (`ModelRequest.tools` is already `list[dict[str, Any]]`).

**Worker wiring** (`apps/worker/forge_worker/agent_runner.py`): `build_agent_runner(*, workspace_id=None, model_client=None)` resolves a real `ModelClient` when (a) one is injected, or (b) provider creds are present (env or vault for `workspace_id`); otherwise it keeps the offline `ScriptedModelClient`. `run_agent_task` resolves per-workspace from the objective's workspace.

### 3.4 Frontend

None. (Cost/usage visibility surfaces through the existing observability views; a usage badge in the Approval/run UI is out of scope — noted in §12.)

### 3.5 Infra / deploy / CI

- **Deps:** add `anthropic>=0.49` and `openai>=1.60` as an optional extra `forge-agent[providers]` in `packages/agent-runtime/pyproject.toml`. Re-lock (`uv lock`) is owned by HARD-14; HARD-02 declares the deps.
- **`.env.example` / `.env.integration.example`:** the model keys already exist as `MODEL_PROVIDER` / `MODEL_PROVIDER_KEY`; HARD-02 standardizes the live-lane names (`FORGE_MODEL_PROVIDER`, `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`, `FORGE_MODEL_NAME`, `FORGE_MODEL_EFFORT`, `FORGE_MODEL_MAX_TOKENS`, `FORGE_MODEL_TIMEOUT_S`, `FORGE_MODEL_MAX_RETRIES`, `FORGE_MODEL_BASE_URL`, `FORGE_MODEL_PROMPT_CACHE`) and documents them in `.env.integration.example` (names only; the real `.env.integration` stays gitignored).
- **CI:** the integration lane runs `uv run pytest -m integration` only when `FORGE_MODEL_PROVIDER` + the matching key are present (a CI secret); the default `uv run pytest -q` lane runs everything **except** `-m integration` so it stays network-free. No change to the hermetic gate.

## 4. Public interfaces / contracts

**Frozen, conformed to (not changed)** — `forge_contracts`:
```python
class ModelClient(Protocol):                 # protocols.py
    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]: ...

class ModelMessage(_Model):  role: str; content: str
class ModelToolCall(_Model): id: str | None; name: str; arguments: dict[str, Any]
class TokenUsage(_Model):    input_tokens: int = 0; output_tokens: int = 0
class ModelRequest(_Model):  model: str; messages: list[ModelMessage]; system: str | None
                             tools: list[dict[str, Any]]; max_tokens: int | None
                             temperature: float | None; stop: list[str] | None
                             metadata: dict[str, Any]
class ModelResponse(_Model): content: str; model: str | None; stop_reason: str | None
                             tool_calls: list[ModelToolCall]; usage: TokenUsage | None
class ModelStreamEvent(_Model): type: str; text: str | None; delta: str | None
```

**New (`forge_agent.providers`):**
```python
class ProviderName(StrEnum):
    anthropic = "anthropic"; openai = "openai"

@dataclass(frozen=True)
class ModelClientConfig:
    provider: ProviderName
    model: str                         # e.g. "claude-opus-4-8"
    api_key: str                       # resolved from vault/env; never logged
    effort: str = "high"               # low|medium|high|xhigh|max (anthropic)
    max_tokens: int = 16000
    timeout_s: float = 600.0
    max_retries: int = 2
    base_url: str | None = None
    prompt_cache: bool = True          # anthropic system-prompt caching
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModelClientConfig | None": ...

class ModelClientError(RuntimeError): ...          # raised on redacted provider failure
class ModelClientUnavailable(ModelClientError): ...  # SDK not installed / no creds (skip on integration lane)

def build_model_client(
    config: ModelClientConfig, *, redactor: Callable[[str], str] = lambda s: s,
    client: Any | None = None,                      # inject SDK client / mock transport for tests
) -> ModelClient: ...

class AnthropicModelClient:  # implements ModelClient
    def __init__(self, *, model: str, api_key: str, effort: str = "high",
                 max_tokens: int = 16000, timeout_s: float = 600.0, max_retries: int = 2,
                 base_url: str | None = None, prompt_cache: bool = True,
                 redactor: Callable[[str], str] = lambda s: s, client: Any | None = None) -> None: ...
    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]: ...

class OpenAIModelClient:     # implements ModelClient  (same constructor shape sans effort/prompt_cache)
    ...
```

**Pricing / usage:**
```python
MODEL_PRICING: dict[str, tuple[float, float]]   # model -> (input_usd_per_mtok, output_usd_per_mtok)
def cost_usd(model: str, usage: TokenUsage, *, cache_read_tokens: int = 0) -> float: ...

class UsageAccumulator:
    def add(self, usage: TokenUsage | None, *, cache_read_tokens: int = 0) -> None: ...
    def to_artifact(self, model: str) -> dict[str, Any]: ...   # {input_tokens, output_tokens, cost_usd, calls, cache_read_input_tokens}
```

**Resolver (`apps/api/forge_api/auth/service.py`):**
```python
def resolve_model_client(self, workspace_id: uuid.UUID, *, secret_id: uuid.UUID | None = None,
                         redactor: Callable[[str], str] = redact_text) -> ModelClient: ...
```

**Env vars / config keys (names only; values in gitignored `.env.integration`):**
`FORGE_MODEL_PROVIDER` (anthropic|openai), `FORGE_MODEL_NAME`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `FORGE_MODEL_EFFORT`, `FORGE_MODEL_MAX_TOKENS`, `FORGE_MODEL_TIMEOUT_S`, `FORGE_MODEL_MAX_RETRIES`, `FORGE_MODEL_BASE_URL`, `FORGE_MODEL_PROMPT_CACHE`. BYOK key alternatively resolved from the vault under `APIKeyKind.MODEL_PROVIDER`.

## 5. Dependencies

- **Foundation (must exist — present in ALPHA):** frozen `forge_contracts.ModelClient` + the `Model*` DTOs; `forge_agent.AgentRunner` + `forge_agent.testing.ScriptedModelClient`; `apps/worker.agent_runner`; `forge_api.auth.vault.SecretVault` + `APIKeyKind.MODEL_PROVIDER`; `forge_api.observability.redaction.redact_text`; the `integration` pytest marker (already declared in `pyproject.toml`).
- **HARD-10 (Production crypto + OAuth seam) — REQUIRED before the live lane is trustworthy.** Per the spec sequencing, the real `FernetCipher`/`FORGE_SECRET_KEY`-backed vault must exist before BYOK keys flow through it (HARD-02/03/05/06/07 all resolve keys from the vault). HARD-02's code works against the existing in-memory `SecretVault` for unit tests; the *production* key path depends on HARD-10.
- **HARD-01 (Real Postgres + pgvector) — SOFT.** Only needed if per-run usage/audit is persisted to `forge_db`; the in-`artifacts` usage path and the live agent run need no DB.
- **HARD-03 (real embedder) — SIBLING, not a dependency.** HARD-02 provides the shared BYOK key resolution + provider config the embedder consumes; embedder quality/eval is HARD-03/04.
- **HARD-11b (LangGraph verified on the real path) — CONSUMER.** HARD-11 verifies the real `langgraph.StateGraph` + `recursion_limit → GraphError` semantics *on the live model path* established here; it depends on HARD-02, not the reverse.
- **External:** a real BYOK Anthropic *or* OpenAI key for the live integration test (supplied via `.env.integration`). The `anthropic` / `openai` SDKs (optional extra).

## 6. Acceptance criteria

1. `AnthropicModelClient` and `OpenAIModelClient` each satisfy the frozen `ModelClient` Protocol (`isinstance(client, ModelClient)` via `@runtime_checkable`); `complete()` and `stream()` are implemented. *(offline — injected mock transport / fake SDK client)*
2. `build_model_client(ModelClientConfig(provider=anthropic, …))` returns an `AnthropicModelClient`; `provider=openai` returns an `OpenAIModelClient`; an unknown provider raises `ModelClientError`. `ModelClientConfig.from_env` parses the documented env vars and returns `None` when no provider/key is configured. *(offline)*
3. Forge `ModelRequest` round-trips through each adapter's translator: a request with a system prompt, multi-turn messages, and tool schemas (incl. the new `input_schema`) produces a provider-shaped request; a provider response with one tool-use block maps to exactly one `ModelToolCall(id, name, arguments)`; a text-only response maps to `ModelResponse.content`. *(offline)*
4. Usage accounting: after a `complete()` returning `TokenUsage(input_tokens=N, output_tokens=M)`, `UsageAccumulator.to_artifact(model)` reports `input_tokens==N`, `output_tokens==M`, `calls==1`, and `cost_usd` equal to the hand-computed `cost_usd(model, usage)` from `MODEL_PRICING`. *(offline)*
5. Per-run aggregation: an `AgentRunner` run over a fake client that returns usage on each turn produces `AgentRunResult.artifacts["model_usage"]` with summed token totals and `calls == number of model turns`. *(offline)*
6. Refusal handling: a fake/adapter response with `stop_reason == "refusal"` makes the runner finish with `RunStatus.ESCALATED`, `needs_human is True`, a `risks` entry naming the refusal, and **no** further model call (no blind retry). *(offline)*
7. Redaction (offline): when the injected SDK client raises an exception whose message contains a `Bearer <token>` / `sk-…` / `ghp_…`, the re-raised `ModelClientError` message contains `[REDACTED]` and not the secret substring; the adapter logs no request/response body. Asserted with the real `redact_text` as the injected redactor.
8. No-creds safety: `build_agent_runner()` with no provider creds returns a runner backed by `ScriptedModelClient`; the worker task still completes. *(offline)*
9. **Live (requires real creds):** `@pytest.mark.integration` — a minimal `AgentObjective` runs to a terminal state through `AgentRunner` against the provider named by `FORGE_MODEL_PROVIDER` using the env BYOK key; the result `status` is terminal (`SUCCEEDED`/`ESCALATED`), the real `langgraph.StateGraph` executed (steps include real model `thought`s), and the bounded-loop/`GraphError` backstop is intact. Skips cleanly with a clear reason when creds/SDK are absent.
10. **Live (requires real creds):** token + cost are recorded — `artifacts["model_usage"]["input_tokens"] > 0` and `cost_usd >= 0`, and the usage is emitted to the observability path (span/usage record).
11. **Live (requires real creds):** redaction on the live path — no API key (or any `redact_text`-matching secret) appears in any step `thought`/`observation`, trace, audit row, or captured worker log from the run.
12. **Live (requires real creds):** provider swap — the same objective run with `FORGE_MODEL_PROVIDER=openai` (key present) also reaches a terminal state, proving the seam is provider-agnostic. *(skipped if only one provider's creds are present)*
13. Whole-suite green gate: `uv run pytest -q` (default lane, network-free, integration tests skipped), `uv run ruff check .`, `uv run ruff format --check .`, and `make typecheck` all pass at the end of the workstream; no real secret appears in source, fixtures, or the lockfile.

## 7. Test plan (TDD) — unit + integration

Write tests first. Default-lane tests are hermetic (no network, no SDK required — adapters tested via an injected fake SDK client / `httpx.MockTransport`, mirroring `HttpEmbeddingClient`'s test approach). Live tests are `@pytest.mark.integration` and creds-gated.

**Unit (offline) — `packages/agent-runtime/tests/`:**
- `test_providers_protocol.py` — both adapters are `ModelClient` instances; factory routing (AC1, AC2).
- `test_providers_translate.py` — request/response + tool-schema mapping, tool-use → `ModelToolCall`, text → content (AC3); built against recorded provider-shaped JSON fixtures (no secrets).
- `test_providers_usage_cost.py` — `UsageAccumulator` + `cost_usd` from `MODEL_PRICING` (AC4); per-run aggregation into `artifacts["model_usage"]` via `AgentRunner` over a usage-returning fake (AC5).
- `test_providers_refusal.py` — `stop_reason="refusal"` → `RunStatus.ESCALATED`, `needs_human`, no retry (AC6).
- `test_providers_redaction.py` — injected SDK client raises with an embedded secret; `ModelClientError` is redacted; no body logged (AC7), using real `forge_api.observability.redaction.redact_text` as the redactor.
- `test_providers_config_env.py` — `ModelClientConfig.from_env` parses/omits correctly (AC2).
- Extend `apps/worker/tests/test_agent_runner.py` — no-creds → scripted client; injected real client used when present (AC8).

Fixtures: `FakeAnthropicSDK` / `FakeOpenAISDK` (return canned `messages.stream`/`chat.completions` objects exposing `get_final_message()` / `.choices`/`.usage`), and recorded provider response JSON under `tests/fixtures/providers/` (scrubbed, no real ids/keys).

**Integration (creds-gated) — `packages/agent-runtime/tests/test_providers_live.py` and `apps/worker/tests/test_agent_runner_live.py`:**
- `test_live_agent_run_reaches_terminal_state` (AC9) — skip unless `FORGE_MODEL_PROVIDER` + key + SDK present; run a 1–2 turn objective; assert terminal status and real steps.
- `test_live_usage_recorded` (AC10), `test_live_redaction_holds` (AC11) — assert on the captured trace/logs.
- `test_live_provider_swap` (AC12) — parametrized over available providers.
Each uses a module-level skip helper that reads env and `importlib.util.find_spec("anthropic"|"openai")`, skipping with the exact missing-cred/SDK reason (never falling back to a fake on the integration marker).

**How to run:**
```bash
# Hermetic default lane (network-free; integration skipped):
uv run pytest -q -m "not integration"
uv run pytest packages/agent-runtime -q

# Live lane (needs .env.integration with a real BYOK key + the providers extra):
uv sync --extra providers          # installs anthropic/openai
set -a; source .env.integration; set +a
uv run pytest -m integration packages/agent-runtime apps/worker
```

## 8. Security & policy considerations

- **Env-only key ingress.** BYOK keys come from process env (sourced from gitignored `.env.integration`) or the encrypted per-workspace vault (`APIKeyKind.MODEL_PROVIDER`); never from source, fixtures, or CI logs. `.gitignore` already ignores `.env`, `.env.*` (except `*.example`).
- **Resolved per call, discarded.** `resolve_model_client` / `build_model_client` read the key at construction, pass it straight to the SDK client, and keep it only inside the client instance — never in module globals, never echoed in `repr`, never written to a step/trace/audit row.
- **Redaction is defensive and shared.** Adapters never log request/response bodies; they log only metadata + the SDK `_request_id`. Any provider exception is passed through the injected `redact_text` before re-raise. The runtime's step `thought`/`observation` content is model output (not the key); the observability trace assembler re-applies `redact_text` as it already does for MCP/audit, so even an adversarial model echo of a secret-shaped string is scrubbed.
- **No prompt-cache / metadata leakage.** Anthropic prompt-cache breakpoints carry no secret; provider `metadata` we send (request ids) contains no key.
- **Refusal is fail-safe, not retry-storm.** A `refusal` stop escalates to a human and stops; it does not blind-retry (avoids burning tokens and avoids re-sending a flagged prompt). The refusal *category* is logged, not the content.
- **Bounded cost / loop.** `max_iterations` (runner) and `recursion_limit → GraphError` (graph) bound model calls per run; `max_tokens`, `timeout_s`, and `max_retries` bound each call. `MODEL_PRICING` makes spend observable per run.
- **Policy unchanged.** Tool dispatch stays policy-gated (`ActionPolicyGate`); HARD-02 changes *who decides the tool calls* (a real model) but not the gate — denied tool calls are still denied and surfaced to the model as observations.
- **Tenant isolation.** Vault reads are workspace-scoped (`SecretVault.get_secret(workspace_id, …)`); one workspace's BYOK key is never resolvable from another's run.

## 9. Effort & risk

**Effort: M.** Two SDK adapters (S each given official SDKs), the factory/config/translate/usage/pricing modules (S), runtime usage+refusal extensions (S), worker/api wiring (S), and the test matrix incl. the creds-gated lane (M).

Risks:
- **Frozen-contract fit.** `TokenUsage` carries only token counts; cost and cache-read counts must live outside the DTO (artifacts/observability). Mitigation: explicitly route cost/cache through `UsageAccumulator` + the open `artifacts` dict — do not touch `forge_contracts`. (Low)
- **Tool-schema gap.** `ToolRegistry.schemas()` currently omits `input_schema`; without it providers get name+description only and may produce malformed tool args. Mitigation: additive `input_schema` on tool definitions (non-breaking). (Medium)
- **SDK/API drift.** Anthropic 4.x rejects `budget_tokens`/sampling params and defaults thinking display to omitted; OpenAI tool/usage shapes evolve. Mitigation: pin SDK majors, use adaptive thinking + effort (no `budget_tokens`), parse usage defensively, cover with recorded-fixture tests. (Medium)
- **Refusal/safety false positives on benign work.** Opus 4.8 classifiers can decline adjacent-but-benign tasks. Mitigation: escalate-to-human path (not failure); document; optional server-side fallback is **out of scope** here (noted §12). (Low–Medium)
- **Cost of the live lane.** Real runs cost tokens. Mitigation: the live test uses a 1–2 turn minimal objective and low `max_tokens`; the lane is creds-gated and off by default. (Low)
- **CANNOT be done in-sandbox.** The live runs (AC9–AC12) require a real BYOK key and outbound network — they run only on the integration lane / a networked CI runner, never in the no-network sandbox. A 3rd-party human security review of the BYOK key-handling path is part of HARD-09's pentest punch-list, **not** performable by the build agents.

## 10. Key files / paths (real monorepo)

- `packages/agent-runtime/forge_agent/providers/__init__.py` — public `build_model_client`, `ModelClientConfig`, `ProviderName`.
- `packages/agent-runtime/forge_agent/providers/config.py` — `ModelClientConfig`, `ProviderName`, `from_env`.
- `packages/agent-runtime/forge_agent/providers/base.py` — `build_model_client`, `ModelClientError`, `ModelClientUnavailable`.
- `packages/agent-runtime/forge_agent/providers/anthropic_client.py` — `AnthropicModelClient` (official `anthropic` SDK).
- `packages/agent-runtime/forge_agent/providers/openai_client.py` — `OpenAIModelClient` (official `openai` SDK).
- `packages/agent-runtime/forge_agent/providers/translate.py` — `ModelRequest`↔SDK + tool-schema mapping.
- `packages/agent-runtime/forge_agent/providers/usage.py` — `UsageAccumulator`.
- `packages/agent-runtime/forge_agent/providers/pricing.py` — `MODEL_PRICING`, `cost_usd`.
- `packages/agent-runtime/forge_agent/runtime.py` — extend `_call_model`/`_route`/`_to_result` (usage aggregation + refusal handling).
- `packages/agent-runtime/forge_agent/tools.py` — extend `ToolRegistry.schemas()` with `input_schema`.
- `packages/agent-runtime/pyproject.toml` — add `[providers]` extra (`anthropic`, `openai`).
- `packages/agent-runtime/tests/test_providers_*.py`, `.../test_providers_live.py` — unit + integration.
- `apps/worker/forge_worker/agent_runner.py` — `build_agent_runner(workspace_id=…, model_client=…)`, real resolution.
- `apps/worker/tests/test_agent_runner.py`, `.../test_agent_runner_live.py`.
- `apps/api/forge_api/auth/service.py` — `resolve_model_client(...)`.
- `apps/api/forge_api/auth/vault.py` (read-only use), `apps/api/forge_api/observability/redaction.py` (redactor source).
- `packages/spec-engine/forge_spec/engine.py` — optional `ModelClient` injection for `spec_clarify`/`spec_plan` (additive).
- `packages/knowledge-core/forge_knowledge/embeddings.py` — consume the shared BYOK key/provider config (resolution only; quality is HARD-03).
- `.env.example`, `.env.integration.example`, `conftest.py` (existing `integration` marker), `.github/workflows/ci.yml` (integration lane).

## 11. Research references

- claude-api skill (this session): official Anthropic Python SDK usage, `claude-opus-4-8` default, **adaptive thinking** (`{"type":"adaptive"}` — `budget_tokens` 400s on Opus 4.8), `output_config.effort` (low|medium|high|xhigh|max), **default to streaming + `get_final_message()`** for large `max_tokens` (HTTP-timeout guard), `usage.input_tokens/output_tokens` + `cache_read_input_tokens`, SDK `timeout`/`max_retries` (auto-retry 429/5xx), `stop_reason == "refusal"` + `stop_details.category` handling, prompt-caching (`cache_control: ephemeral`) as a cost lever.
- OpenAI Python SDK: `chat.completions.create(..., stream=True)`, `tool_calls`, `usage.prompt_tokens/completion_tokens`, `timeout`/`max_retries` client kwargs.
- Forge ground truth: `docs/MORNING_REPORT.md` §5(4) (live model path never called), §6 (provider/transport realism), §7(4); `docs/FORGE_SPEC.md` (Agent runtime, Security → secret redaction, BYOK vault); `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` (BYOK env/redaction precedent; `HttpEmbeddingClient` mock-transport test pattern).
- Hardening spec: `SPEC-PRODUCTION-HARDENING.md` → HARD-02 goal/gate, "Credentials & secrets handling" (env-only, vault-resolved, redacted, creds-gated skip-clean), sequencing (HARD-10 before creds-bearing workstreams; HARD-11b consumes HARD-02).
- In-repo contracts/impl read for this slice: `forge_contracts.protocols.ModelClient`, `forge_contracts.dtos.{ModelRequest,ModelResponse,ModelMessage,ModelToolCall,TokenUsage,ModelStreamEvent}`, `forge_agent.runtime.AgentRunner`, `forge_agent.graph` (LangGraph adapter), `forge_agent.testing.ScriptedModelClient`, `forge_api.auth.vault.SecretVault`, `forge_api.observability.redaction`.

## 12. Out of scope / future

- **Real embedder/reranker quality + RAG eval** — HARD-03 (real `sentence-transformers` + BYOK reranker) and HARD-04 (honest recall@k/MRR/nDCG). HARD-02 only supplies the shared BYOK key resolution the embedder reuses.
- **LangGraph-on-real-path verification** — HARD-11b verifies `recursion_limit → GraphError` and the real `StateGraph` against the live model established here.
- **Server-side refusal fallbacks / second-model rescue** (Anthropic `fallbacks` beta) — possible cost/robustness upgrade; HARD-02 escalates to a human instead.
- **Task budgets** (`output_config.task_budget`, beta) and **context editing/compaction** for very long agent loops — future cost/longevity tuning.
- **Persisted per-run usage rows** in `forge_db` (vs. in-`artifacts`) and a usage/cost badge in the Approval/run UI — land with HARD-01 / a UI slice.
- **Vault cipher hardening / `FORGE_SECRET_KEY` enforcement** — HARD-10.
- **Per-workspace model routing policy** (which model/effort per task class) and **prompt-cache pre-warming** — future product tuning.
- **Managed Agents / Anthropic-hosted agent loop** — Forge runs its own loop (`forge_agent`); the managed-agents surface is not used.
