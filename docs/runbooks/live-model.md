# Runbook — Live BYOK model provider (HARD-02)

The agent runtime (`forge_agent.AgentRunner`) runs its plan→act→observe loop
against a **provider-agnostic BYOK model client**. Two real adapters ship behind
the frozen `forge_contracts.ModelClient` seam:

- `AnthropicModelClient` — official `anthropic` SDK (reference/default provider,
  default model `claude-opus-4-8`, adaptive thinking + `output_config.effort`,
  streaming with `get_final_message()`).
- `OpenAIModelClient` — official `openai` SDK (operator picks `FORGE_MODEL_NAME`).

The provider SDKs are an **optional extra** (`forge-agent[providers]`) imported
lazily inside the adapters, so the default hermetic suite needs neither installed
and runs network-free. This runbook covers turning the live path on.

---

## 1. When it activates

`FORGE_MODEL_PROVIDER` is the master switch. With it **unset**, the worker keeps
the offline deterministic `ScriptedModelClient` — the agent still runs end-to-end
(degraded, no network). Set it (plus a BYOK key) and the worker resolves a real
client per run:

```
build_agent_runner()               # apps/worker/forge_worker/agent_runner.py
  1. injected model_client?  -> use it
  2. ModelClientConfig.from_env() -> real AnthropicModelClient / OpenAIModelClient
  3. otherwise                -> ScriptedModelClient (offline)
```

If the provider SDK extra is missing while creds are present, the build raises
`ModelClientUnavailable` and the worker degrades to the scripted client rather
than crashing.

The API-side, vault-backed path is
`forge_api.auth.service.AuthService.resolve_model_client(workspace_id, secret_id=…)`:
it reads the BYOK key from the per-workspace vault (`APIKeyKind.MODEL_PROVIDER`),
merges the `FORGE_MODEL_*` provider/limits from env, and returns a client. The
key is read per call, handed straight to the SDK client, and never held in a
module global, logged, or written to a trace/audit row.

---

## 2. Configure credentials (never commit values)

Copy the template and fill in a real key. `.env.integration` is gitignored.

```bash
cp .env.integration.example .env.integration
# edit .env.integration:
#   FORGE_MODEL_PROVIDER=anthropic
#   ANTHROPIC_API_KEY=sk-ant-…            (or OPENAI_API_KEY + FORGE_MODEL_NAME for openai)
#   FORGE_MODEL_MAX_TOKENS=1024           (keep the live smoke run cheap)
```

Env vars (names only — values live in the gitignored `.env.integration` or the
per-workspace vault):

| Var | Meaning |
|---|---|
| `FORGE_MODEL_PROVIDER` | `anthropic` \| `openai` (master switch) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | provider BYOK key (or `FORGE_MODEL_API_KEY`) |
| `FORGE_MODEL_NAME` | model id (anthropic default `claude-opus-4-8`; required for openai) |
| `FORGE_MODEL_EFFORT` | `low\|medium\|high\|xhigh\|max` (anthropic) |
| `FORGE_MODEL_MAX_TOKENS` | per-response cap (default 16000) |
| `FORGE_MODEL_TIMEOUT_S` | SDK request timeout (default 600) |
| `FORGE_MODEL_MAX_RETRIES` | SDK auto-retries on 429/5xx (default 2) |
| `FORGE_MODEL_BASE_URL` | optional gateway/proxy base URL |
| `FORGE_MODEL_PROMPT_CACHE` | anthropic system-prompt caching (default true) |

---

## 3. Install the extra + run the live lane

```bash
uv sync --extra providers                 # installs anthropic + openai
set -a; source .env.integration; set +a   # export the BYOK creds
export FORGE_MODEL_PROVIDER=anthropic FORGE_MODEL_MAX_TOKENS=1024

# Live model lane (creds-gated; skips cleanly without creds/SDK):
uv run pytest -m live_model packages/agent-runtime apps/worker -q
```

The live tests (`packages/agent-runtime/tests/test_providers_live.py`,
`apps/worker/tests/test_agent_runner_live.py`) assert:

- **G-MODEL / AC9** — a minimal `AgentObjective` runs to a terminal state
  (`SUCCEEDED`/`ESCALATED`) through the real `langgraph.StateGraph`.
- **AC10** — `artifacts["model_usage"]["input_tokens"] > 0` and `cost_usd >= 0`.
- **AC11** — no BYOK key (or any `redact_text`-matching secret) appears anywhere
  in the run trace.
- **AC12** — the same objective routes through `openai` with no code change when
  that provider's creds are present (parametrized over available providers).

The default lane stays hermetic:

```bash
uv run pytest -q -m "not live_model and not integration"   # network-free
```

---

## 4. Cost & safety notes

- **Cost visibility** — per-run token totals + a derived USD cost land in
  `AgentRunResult.artifacts["model_usage"]` (`input_tokens`, `output_tokens`,
  `cost_usd`, `calls`, `cache_read_input_tokens`), priced from
  `forge_agent.providers.pricing.MODEL_PRICING`. Keep `FORGE_MODEL_MAX_TOKENS`
  low for smoke runs.
- **Refusal is fail-safe** — a provider safety stop (`stop_reason == "refusal"`)
  escalates to a human (`RunStatus.ESCALATED`, `needs_human`) with the refusal
  category recorded; the flagged prompt is **not** blindly retried.
- **Redaction** — adapters never log request/response bodies; any provider
  exception passes through the injected `redact_text` before it is re-raised as
  `ModelClientError`, so a key echoed in an error surfaces as `[REDACTED]`.

---

## 5. PARKED — live verification (needs a real BYOK key)

The G-MODEL gate cannot be closed in the no-network sandbox: it needs a real
Anthropic **or** OpenAI key and outbound network. Run the exact command below on
a networked runner with a key configured to close it:

```bash
uv sync --extra providers
set -a; source .env.integration; set +a
export FORGE_MODEL_PROVIDER=anthropic FORGE_MODEL_MAX_TOKENS=1024
uv run pytest -m live_model packages/agent-runtime apps/worker -q
```
