# BYOK & bring-your-own board

Forge is designed to plug into the tools and model providers you already use.
This page covers two connection points:

1. **BYOK** — bring your own model-provider (and reranker) keys.
2. **Bring-your-own board** — sync Forge's work to your existing project-management
   tool instead of (or alongside) the native board.

> Live third-party integrations are **code-complete with tests and runbooks but
> need your keys to verify end to end** — see
> [`RELEASE_READINESS.md`](../../RELEASE_READINESS.md) for the honest status. Store
> every credential through the encrypted vault or your secret manager; never
> commit keys.

## Bring your own key (models & reranker)

The agent runtime calls an LLM provider that **you** supply. Keys are
**envelope-encrypted** in the per-workspace vault (see
[Concepts → Secrets, vault & BYOK](../concepts.md)), never stored in plain
text.

### Model provider

Set the provider and key in `.env` for a quick local run:

```bash
FORGE_MODEL_PROVIDER=anthropic     # anthropic or openai (unset -> offline scripted model)
FORGE_MODEL_API_KEY=<your-api-key> # e.g. sk-ant-... or sk-...; or set ANTHROPIC_API_KEY / OPENAI_API_KEY
# For OpenAI also set the model name:
# FORGE_MODEL_NAME=gpt-4.1
```

For a deployed instance, add the key through the vault instead of the
environment so it is encrypted at rest — the API exposes a secrets endpoint, and
the **Models & Effort** settings screen manages provider configuration per
workspace. The runtime resolves credentials in this order: an explicit vault
secret under `MODEL_PROVIDER`, then the `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`FORGE_MODEL_API_KEY` environment fallbacks.

### Embeddings & reranker

Hybrid retrieval can use a hosted embedding model and an optional reranker:

```bash
EMBEDDING_MODEL=<model>
# Optional reranker (Jina / Cohere / a self-hosted endpoint):
FORGE_RERANK_ENABLED=true
FORGE_RERANK_PROVIDER=jina        # jina | cohere | selfhosted
FORGE_RERANK_MODEL=<model>
FORGE_RERANK_BASE_URL=<url>       # for a self-hosted reranker
JINA_API_KEY=<key>                # or COHERE_API_KEY
```

See the [reranker runbook](../runbooks/live-reranker.md) for the live setup and
the fallbacks when no reranker is configured (retrieval degrades gracefully to
RRF-only ordering).

## Bring your own board

Forge ships a native board, but if your team lives in Jira, Linear, or another
tool, Forge can **sync work to it** through a pluggable **project-management
adapter** seam (`forge_integrations.pm`). Every adapter implements the same
`PMAdapter` contract, so the sync engine, change-hashing, and conflict handling
are identical across providers.

### Supported providers

| Provider | Identifier |
|----------|-----------|
| Jira | `jira` |
| Linear | `linear` |
| Asana | `asana` |
| Monday.com | `monday` |
| GitHub Projects | `github_projects` |
| ClickUp | `clickup` |
| Trello | `trello` |
| GitLab | `gitlab` |
| **Generic** (config-driven, for any other board) | `generic` |

The **generic** connector maps a board's fields declaratively, so you can wire
up a tool that doesn't have a dedicated adapter without writing code.

### Connecting a board

1. Open **Settings → Integrations** in the web UI.
2. Choose your provider and supply its API credentials (stored encrypted in the
   vault).
3. Map your workspace/project and status columns to Forge's states.
4. The **sync engine** reconciles work items in both directions, using content
   hashing to detect changes and a deterministic conflict policy so a two-sided
   edit never silently clobbers one side.

### Adding a provider Forge doesn't ship

Because adapters are a registered seam, adding a new one is: implement a
`PMAdapter`, register it in
[`forge_integrations/pm/registry.py`](../../packages/integration-sdk/forge_integrations/pm/registry.py),
and the sync engine, hashing, and conflict logic come for free. Contributions
are welcome — see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Related

- [Concepts](../concepts.md) — how integrations, secrets, and the board fit
  together.
- [Runbooks](../runbooks) — live setup for GitHub, model providers, the
  reranker, MCP, and Slack.
- [Self-hosting security](../self-hosting/security.md) — secret management and
  hardening for a deployed instance.
