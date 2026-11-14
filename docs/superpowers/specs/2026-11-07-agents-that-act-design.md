# G — Agents that act

**Date:** 2026-11-07
**Status:** Approved, implementation-ready (G1, G2). G3 DRAFT — scope and interfaces only.
**Depends on:** F (local-CLI provider; merged first so G does not miss `ProviderName.local_cli` and
the `ModelClientError` handling in the same files)
**Blocks:** K (automation lines), L (tervin bridge run stream)
**Decisions:** ADR-0007 (production paths fail closed), ADR-0008 (policy-gated tool belt, scoped
credentials, enforced budgets), ADR-0012 (two executor modes, proposed), ADR-0014 (untrusted
content cannot drive privileged tools)

---

## 1. Problem

Forge can describe, govern and audit an agent run, but on its main paths an agent **cannot change
code or open a PR**:

- `POST /agent/runs`, the Celery task `forge.agent.run` and supervised multi-agent runs all build
  `AgentRunner` with an empty `ToolRegistry`. `default_tool_registry(root)` exists in
  `forge_agent/tools.py` and is used by no app.
- No producer in `apps/api` or `packages` sends `forge.agent.run`.
- `POST /agent/runs` runs synchronously inside the request and stores results in an in-memory
  `AgentRunStore`.
- The worker discards `workspace_id` (`del workspace_id`) and resolves the model from env, not the
  workspace vault.
- In the Temporal `FeatureWorkflow`, `build_activities()` passes only a DB store, so `run_agent`,
  `run_checks`, `open_pr`, `notify`, `approval` and `red_team` use defaults that report success —
  including `pr_url = "https://example.invalid/pr/1"`.
- No `run_tests`, `open_pr`, `query_mcp` or `search_knowledge` tool handler exists, although
  `ROLE_TOOLS` names them.

The 2026-09-19 assessment scored Execution and Orchestration "Strong". On the main paths they are
**Partial**. Forge's own rule — "never fake a capability" — already condemns the success defaults.

## 2. Approach

Wire, do not invent. Every piece G needs already exists as a seam: the tool registry, the policy
gate, the sandbox lattice, the GitHub App client, the vault model resolution, the cost meter. G
connects them on the three main paths and makes every unconfigured path refuse.

## 3. G1 — Fail closed, and give agents a tool belt

### 3.1 Production paths fail closed (ADR-0007)

- `forge_workflow/temporal/activities.py`: the default activity implementations raise
  `ActivityNotConfigured(name)` unless `FORGE_ALLOW_FAKE_ACTIVITIES=1` is set. Tests set it
  explicitly. `example.invalid` may appear only in test code; a test greps `apps/` and `packages/`
  (excluding `tests/`) and fails on any occurrence.
- `ScriptedModelClient` fallback in the worker is allowed only under the existing
  `FORGE_ALLOW_SCRIPTED_AGENT=1`; otherwise an unconfigured provider fails the run with
  `stop_reason = provider_unavailable`.

### 3.2 The tool belt

`forge_agent/tools.py` gains handlers for the tools `ROLE_TOOLS` names. Each is a pure function of
`(ToolContext, validated input)` and runs **inside the run's sandbox root**:

| Tool | Behaviour | Network |
|---|---|---|
| `read_file`, `write_file`, `list_dir` | Existing; path-confined to the worktree | none |
| `search_code` | ripgrep over the worktree, 200-line cap | none |
| `run_tests` | Runs the repo's declared test command (`.forge/policy.yaml` `commands.test`, else detection by manifest) with a timeout; returns exit code + last 400 lines | none |
| `run_lint` | Same for `commands.lint` | none |
| `git_commit` | Stages and commits in the run branch with a `Forge-Run: <run_id>` trailer | none |
| `search_knowledge` | Existing retrieval service, workspace-scoped | in-process |
| `query_world` | H1 `query_facts` / `entity_profile`, read-only | in-process |
| `query_mcp` | Existing MCP gateway, read-only connections only | gateway |

`open_pr` is **not** an agent tool. The run's finalizer opens the PR (G2), so an agent can never
open a PR from untrusted content mid-run (ADR-0014).

Every call passes through the existing `ActionPolicyGate` before execution; a denial escalates as
today. Tool scopes are the intersection of role, task and skill allowlists — never widened.

### 3.3 Main paths

| Path | Change |
|---|---|
| `apps/api/forge_api/routers/agent.py` `POST /agent/runs` | Validates, persists an `agent_run` row (table exists), **enqueues** `forge.agent.run`, returns `202` with the run id. The in-memory store is replaced by the table. |
| `apps/worker/forge_worker/agent_runner.py` | Resolves the model through the workspace vault (`AuthService.resolve_model_client`), provisions the sandbox via `build_sandbox_provider` at the workspace's minimum isolation, builds the tool belt for the run's role, runs, finalizes. |
| `apps/worker/forge_worker/multi_agent.py` `_agent_factory` | Builds each subagent's tool belt from `ROLE_TOOLS[role]` inside its own worktree. |
| `apps/worker/forge_worker/temporal_main.py` `build_activities` | Passes real implementations: `run_agent` → the worker runner; `run_checks` → `run_tests` + `run_lint`; `open_pr` → G2 finalizer; `notify` → existing notifier; `approval` → existing approval service. |

### 3.4 Budgets are enforced (ADR-0008)

Before every model call the runner checks the run's budget (tokens and currency) from the existing
cost meter. Exceeding it stops the run with `stop_reason = budget_exhausted`. Local-CLI runs are
metered as *unpriced*, never free, and count against a token cap. Defaults: 200k tokens per run,
overridable per workspace in `.forge/policy.yaml` `budgets.per_run_tokens`.

## 4. G2 — Real PRs with per-run scoped credentials

- The finalizer pushes the run branch and opens the PR through the existing GitHub App client
  (`forge_integrations/github.py`) using an **installation token minted for this run, scoped to the
  one repository, with `contents:write` and `pull_requests:write` only**, expiring at run end
  (ADR-0008). No long-lived token is ever placed in a sandbox; the push happens outside the sandbox
  from the finalizer.
- The PR body carries the run id, the spec key, the model and role, and the attestation digest. The
  `pr` approval gate is created as today; **human approval before merge stays mandatory**.
- CI status is read back through the existing CI integration and attached to the run.
- Three read-only helper tools are added for agents iterating on CI: `ci_failures` (file:line
  list), `ci_job_log` (tail), `pr_status`.

## 4a. G1d — Dispatcher and repository mirrors (implementation-ready)

The worker acts on a local clone named by `objective.context["repo_path"]`. Two pieces connect that
to the board and to Temporal:

- **Mirrors.** `FORGE_REPO_MIRRORS_DIR/<owner>/<name>` holds one clone per connected repository. Before
  a run, the worker fetches it with an installation token scoped to that repository and
  `contents:read` only (same minting path as G2, passed through env config). Runs never share a
  worktree; they share the mirror's object store.
- **Dispatcher.** `build_objective(task, spec, repo_connection) -> AgentObjective` maps a board task
  with an approved spec to an objective: `objective` ← task title, `acceptance_criteria` ← the spec's
  criteria, `allowed_actions` ← `ROLE_TOOLS[implementer]` ∩ the skill profile, `repo_targets` ←
  the task's repository, `context` ← `repo_path`, `repo_full_name`, `workspace_id`,
  `agent_run_id`. The Temporal `run_agent` activity and an automation action ("start agent run when
  a task's spec is approved") both call it and enqueue `forge.agent.run`.

## 4b. Implementation status (2026-11-07, branch `feat/agents-act-g`)

| Item | State |
|---|---|
| G1 §3.1 fail closed: Temporal stand-ins refuse unless `allow_fakes` / `FORGE_ALLOW_FAKE_ACTIVITIES=1`; no `example.invalid` in shipped code (repo test) | done |
| G1 §3.1 worker refuses the scripted client unless `FORGE_ALLOW_SCRIPTED_AGENT` (matches the API) | done |
| G1 §3.2 tool belt: `search_code`, `run_tests`/`run_lint`/`run_typecheck` (declared `policy.commands` only), `git_commit` (hooks off, `Forge-Run` trailer) | done |
| G1 §3.3 worker path: per-run worktree + sandbox from settings strengthened by repo policy + tool belt | done |
| G1 §3.4 budgets: `BudgetedModelClient`, `budget_exhausted` fails the run, `FORGE_AGENT_RUN_TOKEN_BUDGET` | done |
| G2 finalizer: one-repo `contents`+`pull_requests` token, push via env config, PR opened, outcome recorded; failure fails the run | done |
| G1 §3.3 `POST /agent/runs` → 202 + enqueue + `agent_run` row | open |
| G1 §3.3 multi-agent `_agent_factory` tool belts per role | open |
| G1d mirrors + dispatcher; Temporal `build_activities` wired to them | open |
| G2 CI read-back tools (`ci_failures`, `ci_job_log`, `pr_status`) | open |

## 5. G3 — Delegated executor (DRAFT, ADR-0012)

A second executor mode beside the LangGraph loop: run Claude Code (`--output-format stream-json`
with a PreToolUse hook), Codex (`exec --json`) and ACP agents as **full agents inside forge's
container sandbox**. The hook / ACP `session/request_permission` calls forge's policy endpoint and
**fails closed** (tervin's hook fails open with a warning; lore's auto-merge fails open — a server
factory must not). Codex runs without a gate are labelled `observed`, never `gated`. Open question
before production: whether provider terms permit subscription-authenticated CLIs on shared
servers.

## 6. Testing

- Tool unit tests: path confinement (escape attempts refused), timeouts, output caps, commit
  trailer, network absence in the container profile.
- Policy: a denied tool call never executes and escalates (existing gate, new tools).
- Worker integration (SQLite + scripted model + local sandbox): a run edits a file, runs tests,
  commits, and the finalizer is invoked with the branch.
- Temporal: activities without configuration raise; with `FORGE_ALLOW_FAKE_ACTIVITIES=1` the old
  defaults still work for existing tests.
- Repo-wide test: no `example.invalid` outside tests.
- Budget: a scripted model exceeding the cap stops with `budget_exhausted`.
- G2 against a recorded GitHub cassette: token scoped to one repo, PR created, gate created.

## 7. Acceptance criteria

1. `POST /agent/runs` returns 202 and the run executes in the worker with a non-empty tool belt.
2. On `examples/sample-repo`, an approved-spec task produces a real PR on a test repository with CI
   status attached and the `pr` gate required before merge (G2).
3. No production path can emit `example.invalid`; unconfigured activities refuse.
4. A policy-denied tool call is blocked and escalated.
5. A run aborts at its budget.
6. The installation token used for a run cannot write to any other repository.

## 8. What G deliberately does not do

- Give agents network access inside the sandbox.
- Let an agent tool open, approve or merge a PR.
- Change the approval matrix or the policy language.
