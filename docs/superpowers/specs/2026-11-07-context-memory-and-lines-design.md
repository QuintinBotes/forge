# J + K — Context, memory and automation lines (lore++)

**Date:** 2026-11-07
**Status:** J: Approved, implementation-ready (decisions in §2.5). K: Approved, implementation-ready (interfaces settled in §4, ADR-0020).
**Depends on:** J: H1, I. K: G, J.
**Blocks:** programme acceptance (unified-factory-programme §5 items 4–5)
**Decisions:** ADR-0003, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0014, ADR-0017, ADR-0018, ADR-0020

---

## 1. Purpose

lore does two jobs: it gives Claude Code org-wide **context and memory** over MCP, and it
runs **automation lines** that turn tickets into PRs. This spec rebuilds both on forge, to the "better
means" bars in the programme scoreboard (§4 of `2026-11-07-unified-factory-programme.md`).

## 2. J — Context and memory

### 2.1 Context assembly

`assemble_context(task, budget_tokens, template)` returns one token-budgeted bundle:

1. World-model facts about the entities named in the task, holding *now*, with evidence and conflict
   markers showing both sides.
2. Passages from `forge_knowledge` (BM25 + vector + RRF + reranker), **whole requirements never
   truncated** (a requirement block is atomic; it is dropped, not cut).
3. Specs and ADRs with their **status** (draft / approved / superseded).
4. Relevant memories (`layer = memory`) by effective confidence.
5. Repo instruction files (AGENTS.md, CLAUDE.md, `.cursor/rules`) read per tervin's instruction
   readership table.

Templates: `default`, `review`, `implementation`, `research`. Exposed as MCP tool
`assemble_context` and REST `POST /context/assemble`. Every bundle records the ids it contained
(`context_refs`) on the run that requested it.

### 2.2 Memory lifecycle

Memories are world facts with `layer = memory`:

- Written only through proposals or by a person (ADR-0005). Passive capture (session summaries,
  task lessons) produces proposals, opt-in per workspace.
- Expiry is `valid_to`. Effective confidence = `confidence × 0.5^(age / half_life)`, computed on
  read; no cron mutates rows.
- **Outcome feedback that works** (lore #2385 is dead code): when a run's PR merges, each fact in
  its `context_refs` gets a `reinforced` event; when rejected, `weakened`. Half-life is a function of
  those events. The link is written by the production finalizer and covered by an end-to-end test.

### 2.3 Wider index

Ingest issues, PRs (title, body, review threads), commits (message + touched paths), CI failure
summaries, and repo instruction files, through existing PM/GitHub adapters into `forge_knowledge`;
named entities and decisions from them into `forge_world`. A code-symbol index maps symbol → file
(tree-sitter chunking already exists). Idempotent by content hash.

### 2.4 Retrieval quality is measured

- Production defaults replace `DeterministicEmbeddingClient` and the fixture reranker; hash and
  fixture clients become test-only (ADR-0007).
- The BM25 leg is real BM25 (pg_search/ParadeDB or an in-repo BM25 over `tsvector` statistics —
  chosen after licence review); a missing leg is an error, never silent.
- A port of lore's 60-question agent benchmark runs in `forge_eval`. **Bar: ≥ 0.86** (utopia's
  score on lore's set), hallucination ≤ 7%, p50 ≤ 17.8k tokens. A regression fails CI.

### 2.5 Decisions (settled 2026-11-07)

**Lexical leg — real BM25 on Postgres, no new dependency (ADR-0017).** Today `Bm25Store` ranks with
`ts_rank` on Postgres (not BM25; lore has the same flaw) and with real Okapi BM25 only on SQLite.
J replaces the Postgres path with *candidate-then-rescore*:

1. Candidates: chunks matching an **OR** tsquery of the query terms (`to_tsquery('simple', t1 | t2 …)`,
   built from the existing `tokenize`), ordered by `ts_rank_cd`, limit 200, workspace/scope filtered.
   OR semantics keeps BM25's recall; `plainto_tsquery` (AND) loses it. The stored `tsv` is
   `to_tsvector('simple', ' '.join(tokenize(content)))`, so candidates and the rescorer see the same
   tokens: the `english` config kept `self.retry` or `v1.2.3` as one lexeme, stemmed, and dropped
   stopwords, so Postgres missed chunks SQLite found (review of the first pass, 2026-11-07).
   Queries drop one shared English stopword list on both dialects; a stopword-only query returns
   nothing on either.
2. Rescore the candidates with exact Okapi BM25 (k1, b as today) using corpus statistics kept
   incrementally at index time in `retrieval_term_stats(workspace_id, term, df)` and
   `retrieval_chunk.token_count`, plus a per-workspace `chunk_count`/`total_tokens` row. Upserts and
   deletes adjust the counts in the same transaction as the chunk write.
3. One shared `bm25_score(query_terms, doc_terms, df, n_docs, avg_len)` pure function serves both
   dialects, so SQLite and Postgres rank identically on the same corpus (tested).

Migration slug: `retrieval_bm25_stats` (next free number, ADR-0009), with a backfill over existing chunks.

**Vector leg and reranker — configured or explicitly absent, never faked (ADR-0018).**

- Embedder: `HttpEmbeddingClient` (OpenAI-compatible) from `FORGE_EMBEDDING_MODEL`,
  `FORGE_EMBEDDING_BASE_URL` (SSRF-validated with the existing `url_validator` seam),
  `FORGE_EMBEDDING_DIMENSION` (must equal the `retrieval_chunk.embedding` column's
  `EMBEDDING_DIM`, else startup refuses), and the key from `FORGE_EMBEDDING_API_KEY`.
  `SentenceTransformerEmbeddingClient` remains selectable with `FORGE_EMBEDDING_PROVIDER=local`.
- With no embedder configured: search runs **BM25-only** and every response carries
  `legs: ["bm25"]` and `vector_leg: "unconfigured"`; indexing stores chunks without embeddings; any
  route that requires vectors returns 503. `DeterministicEmbeddingClient` is constructed only by
  tests (a repository test forbids it outside test code, like `example.invalid`).
- Reranker: default `FORGE_RERANK_PROVIDER` becomes `none` (no rerank, reported as
  `rerank: "off"`); `fixture` is test-only; `jina`/`cohere`/`selfhosted` unchanged.

**`context_refs`.** A JSON list on `agent_run` of `{"kind": "fact"|"chunk"|"spec"|"memory", "id": str}`,
written by `assemble_context` when called with an `agent_run_id`, and copied into the run result
artifacts. Migration slug `agent_run_context_refs`.

**Memory and outcome feedback.**

- `world_fact_feedback(fact_id, run_id, outcome ∈ {reinforced, weakened}, created_at)` — append-only.
- Effective confidence (computed on read, never stored):
  `min(1, confidence × 0.5^(age_days / half_life_days) × 1.1^reinforced × 0.8^weakened)` with
  `half_life_days = 30` for `layer = memory` and no decay for other layers.
- Trigger: the GitHub `pull_request` webhook already handled by the integration router. A closed PR
  whose head branch is `forge/run-<hex>` (the finalizer's naming) resolves to its `agent_run`;
  `merged = true` writes `reinforced` for every fact/memory ref in `context_refs`, otherwise
  `weakened`. Idempotent per `(run_id, fact_id)`.

**Not now.** `lore_*` MCP aliases: deferred until a lore user asks. The benchmark port (§2.4) follows
J's implementation as its own task.

## 3. K — Automation lines

Lines are forge workflow-DSL definitions with `agent`, `service` and `human` stations, distributed
as signed `workflow_template` marketplace packages (the kind is reserved today).

| Line | Lore source | Forge bar |
|---|---|---|
| Implementation loop | `implementation-loop.yaml` (17 stations) | Parallel tickets with dependency-aware scheduling; failing tests first; lint and tests before push; hard per-run budget; real PR via G2; never merges |
| Code review | `code-review*.yaml` | Fail-closed check run; severity with audit; suggestions verified to apply |
| Triage | `issue-triage.yaml` | Reproduce in sandbox; duplicate detection via world model; Jira/Linear through PM adapters; incidents and alerts as inputs |
| Spec → tickets | `feature-planning.yaml` | From Spec Studio approvals, with stable `R`/`A` ids |
| Spec upkeep | `spec-upkeep.yaml` (weekly) | Continuous on push: a spec approval records a world decision; drift is H1's `drift()` |
| Auto-merge | `auto-merge.yaml` | Shadow-only until programme A measures agreement; **fails closed** when any dependency is unreachable (lore #2374 fails open) |
| Merge bookkeeping | `merge.yaml` | Closes tickets, reinforces context (J §2.2), records the outcome |
| Onboarding | `onboard.yaml` | Idempotent with a verified checklist |
| Digest / changelog | `daily-digest.yaml`, `weekly-changelog.yaml` | By audience; Slack and email; agent-run aware |

Measured bars on a dogfood repository over a comparable batch: merged-PR yield above lore's 43% and
all-in cost per merged PR below lore's ~$105 (self-reported by lore; measured by programme C/D).

## 4. Interfaces (settled 2026-11-08)

A line is a workflow definition in DSL v2. Each non-terminal state carries one station (`agent`, `service` or `human`). Entering a state runs its station. The station's outcome is the event, and the existing `TransitionEvaluator` uses it to choose the outgoing edge. Lines run on a new Temporal workflow, `forge.StationWorkflow`, registered beside `FeatureWorkflow`, which does not change. Lines are installed only from signed `workflow_template` packages, into the F28 definition tables, as read-only, append-only revisions. Decision record: ADR-0020.

### 4.1 DSL v2

A new module, `forge_contracts/lines.py`, holds the line models, the service catalog and the pure `validate_line`. `WorkflowDefinition` is not edited. Keeping these in contracts lets the marketplace SDK validate a line without importing `forge_workflow`, which pulls in `forge-db` and `temporalio`.

`LineDefinition(WorkflowDefinition)`, `extra="forbid"`:

| Field | Rule |
|---|---|
| `dsl_version` | `"2"`, matching `workflow_definition_revision.dsl_version` |
| `workflow`, `version` | As in v1 |
| `initial` | Required. Must be a station state |
| `terminals` | `{state: succeeded \| failed \| cancelled}`. `failed` and `cancelled` are implicit and reserved |
| `budget` | `{max_tokens: int > 0, max_usd: Decimal \| null}`. Required when any agent station exists |
| `concurrency` | `{max_active_runs: 1..50 = 3}`, counted per workspace and definition at start |
| `stations` | `{state: Station}` |
| `transitions` | `[LineTransition]` |

`modes`, `retry_policy` and `escalation_policy` are rejected. Loops are bounded by `max_visits`, and confidence is set per station.

`LineTransition` (`extra="forbid"`) has `from`, `to`, `when` (one outcome token of the `from` station) and an optional `condition`. `action`, `preconditions`, `checks`, `record` and `skill` are rejected.

`parse_definition` reads `dsl_version` from the raw mapping:
- Absent or `"1"`: it returns today's `WorkflowDefinition`. A v1 document that carries `stations`, `terminals`, `budget` or `initial` is an error.
- `"2"`: it returns `LineDefinition`.

State names must match `^[a-z][a-z0-9_]{0,63}$`.

**References.** Station inputs and objective placeholders are paths only, with no expressions:
- `task.{id,key,title,description,repo}`
- `trigger.{external_ref}`
- `run.id`
- `outputs.<state>.<key>`

`task.repo` is the task's single repo target. A line that references it refuses to start (409 `repo_target_ambiguous`) when the task has no repo target or several. Values substituted into an agent prompt carry a source label (ADR-0014).

**Fields common to every station:** `kind`, `title`, and `max_visits` (1..20, default 3). Entering the station once more than `max_visits` gives the outcome `visit_limit`.

**Agent station** (`kind: agent`)
- Required:
  - `role`: a `SubAgentRole`.
  - `skill_profile`.
  - `context_template`: `default | review | implementation | research` (J §2.1).
  - `objective`: at most 8 KiB, with `{{path}}` placeholders. An unset path renders empty.
- Optional:
  - `model`: checked against the workspace allowlist at start.
  - `decisions`: a closed set. The agent must return one value, and that value becomes the outcome instead of `succeeded`.
  - `confidence_threshold`: default 0.72.
  - `timeout_seconds`: default 7200, at most 14400.
  - `budget {max_tokens, max_usd}`: at most the run budget.
- Tools are `ROLE_TOOLS[role]` ∩ the skill profile, minus `{open_pr, write_review_comment}`. The sandbox network is off, and no field can turn it on.
- Outcomes: `succeeded` (or a decision value), `failed`, `awaiting_input`, `low_confidence`, `budget_exhausted`, `timed_out`, `visit_limit`.
- Outputs: `summary_ref`, `branch`, `head_sha`. Agent outputs may feed objectives and service inputs marked `trust: any`. They are never guards and never feed inputs marked `trust: task`.

**Service station** (`kind: service`)
- Fields:
  - `service`: a catalog name.
  - `inputs {param: path}`.
  - `params {param: JSON scalar}`: validated against the catalog's params model.
  - `timeout_seconds`: at most the catalog maximum.
  - `retry {max_attempts}`: at most the catalog maximum, and forced to 1 when the entry is not idempotent.
- Outcomes: `succeeded`, the entry's declared extra outcomes, `failed`, `timed_out`, `not_configured`, `visit_limit`. An input path that does not resolve gives `failed` with `error_code = input_unresolved`, before the service is called.
- Outputs: only the keys the catalog declares, typed, capped at 16 KiB. A boolean output can be used as a guard, written `condition: <state>.<key>`.

**Human station** (`kind: human`)
- `gate`: `spec | plan | pr`, a subset of the frozen `ApprovalGate`. `deploy`, `incident_remediation` and `policy_override` are rejected, because their hooks act on approval.
- `required_approvals`: 1..5. Repo policy can still raise it.
- `sla_seconds`: required, 300..2,592,000.
- `summary`: a template.
- Outcomes: `approved`, `rejected`, `changes_requested`, `expired`, `visit_limit`.

**Service catalog.** `SERVICE_CATALOG` holds metadata only. Each entry has these fields:
- `name`
- `params_model`, with no secret fields
- `inputs {name: trust task|any}`
- `outputs {name: type}`
- extra `outcomes`
- `max_timeout_seconds` and `max_attempts`
- `idempotent`
- `effect: read | write_external`
- `credential`: a vault reference name, or none

Service bodies are bound in the worker composition root through the `pick()` pattern (`temporal/activities.py:142-199`). An unbound name raises the non-retryable `ActivityNotConfigured`, which becomes the outcome `not_configured`. A catalog name never changes meaning: a breaking change ships under a new name. Phase-1 entries:

| Name | Effect | Extra outcomes | Notes |
|---|---|---|---|
| `forge.prepare_worktree` | read | | One worktree per run, keyed by run id. Agent stations run in it |
| `forge.confirm_red` | read | `not_red` | `succeeded` only when the new tests fail and the existing tests pass |
| `forge.run_checks` | read | `checks_failed` | Lint and tests from `.forge/policy.yaml`. No declared command means `failed`. Output `failure_summary` |
| `forge.open_pr` | write_external | | The G2 finalizer. Opens a draft PR on branch `forge/run-<hex>` with a per-run token. Output `pr_ref` |
| `forge.push_branch` | write_external | | Pushes only to the run branch |
| `forge.await_ci` | read | `checks_failed`, `no_checks` | Polls inside the activity with heartbeats. Zero check runs gives `no_checks`, never green. Output `failure_summary` |
| `forge.mark_pr_ready` | write_external | | |
| `forge.tracker_comment` | write_external | | Comments only. Never changes tracker status |

No merge-capable entry exists.

**Routing and defaults.** The interpreter calls `TransitionEvaluator.resolve(state, outcome)`. An outcome with no edge takes a default:
- **Park** for `awaiting_input`, `low_confidence`, `expired`, `visit_limit` and `no_checks`. The run stays in its state, its status becomes `escalated`, and `context.parked = {state, visit, reason}` is recorded.
- **Fail** for `failed`, `timed_out`, `not_configured` and `budget_exhausted`, and when a guard leaves no edge enabled. The run moves to `failed`.

**Validation.** `validate_line` is pure. It runs when the editor publishes, at install, and at run start. These are errors:
- `STATE_WITHOUT_STATION`: a non-terminal state has no station.
- `DEAD_END_STATE`: a state has no outgoing edge and is not in `terminals`. A mistyped `to:` fails here instead of ending the run.
- `UNROUTED_SUCCESS`: `succeeded`, a decision value or `approved` has no edge.
- `FAIL_OPEN_ROUTE` (lore #2374):
  - after a failure-class outcome, a path reaches a `succeeded` terminal without running the failing station again, however many states it passes through. The failure-class outcomes are `failed`, `timed_out`, `rejected`, `expired`, `visit_limit`, `no_checks`, `changes_requested`, `low_confidence`, `awaiting_input`, and each catalog entry's own failure outcomes (`checks_failed` for `forge.run_checks` and `forge.await_ci`, `not_red` for `forge.confirm_red`). A failure may share a `failed` or `cancelled` ending, or a notify step, with a decision;
  - any edge exists on `not_configured` or `budget_exhausted`.
- `VISIT_LIMIT_CYCLE`: `visit_limit` edges lead back to their own station. Past their limits, those stations would hand the run around forever without doing any work.
- `UNKNOWN_SERVICE`, `STATION_PARAMS_INVALID`, `UNKNOWN_OUTCOME`, `UNKNOWN_REFERENCE`, `AGENT_STATION_UNBUDGETED`, `INVALID_HUMAN_GATE` and `RETRY_GUARD_IN_V2`, plus the existing reachability and nondeterminism checks.
- `LINE_INVARIANTS`, always applied, whatever `base_bundled_name` is:
  - `no_merge`: no merge-capable service is reachable. If one is ever registered, every path to it must cross the `approved` edge of a `gate: pr` human station.
  - `agent_no_write_tools`: agent tools never include `open_pr`, `push`, `merge`, `approve` or `write_review_comment`.
  - `trusted_inputs`: catalog inputs marked `trust: task` take only `task.id`, `task.key`, `task.repo`, `trigger.external_ref`, `run.id`, or a service output the catalog labels `trust: task` (such as `pr_ref`). `task.title`, `task.description`, agent outputs and CI text such as `failure_summary` are `trust: any`. Untrusted issue text therefore cannot redirect where a finalizer writes (ADR-0014).
  - `agent_outputs_not_guards`.
- `FEATURE_INVARIANTS` apply whenever the invariant's `from_state` appears in the graph. This applies on the v2 and marketplace paths only. The v1 editor path is unchanged.

### 4.2 Execution

**Start.** `POST /workflow/runs {task_id, definition?, trigger?}`.
- An explicit `definition` resolves through `ResolvingDefinitionProvider`, which is now wired in the API composition root. An unknown name returns 404 `unknown_definition` and never falls back to `default_feature`. Omitting `definition` keeps today's behaviour.
- For a v2 revision, start does three things:
  - It re-runs `validate_line`. A forge release with stricter rules stops new runs of an old line with 409 `line_invalid`.
  - It checks `concurrency.max_active_runs` (409 `line_concurrency_exceeded`).
  - It writes `workflow_run.definition_revision_id`.
- The V1 engine refuses v2 with 503 `durable_engine_required` (ADR-0007). The `default_feature` parity tests are unchanged.

**Workflow.** `forge.StationWorkflow`, with id `wf-<run_id>` and `REJECT_DUPLICATE`.
- Params: run id, workspace id, a task snapshot, the trigger, the revision id, and the revision's `dsl_yaml`. Replay therefore never reads the database. History is encrypted by `RedactingEncryptionCodec`.
- A `dsl_version` the worker does not know fails the run without retry.

The loop:
1. `visit = visits[state] + 1`. If this passes `max_visits`, the outcome is `visit_limit`.
2. For an agent station, check the run budget first. If it is exhausted, the outcome is `budget_exhausted` and the agent is not called.
3. `persist_station(running)`, run the station, then `persist_station(done)`.
4. Route the outcome with `TransitionEvaluator(definition, state_cast=str)`. The default `state_cast=WorkflowState` keeps the feature path byte-identical. Service boolean outputs reach guards through a new `PureGuardContext.station_flags`.
5. `persist_transition`, with `run_status` taken from `terminals`.

The visit id is `uuid5(run_id, f"{state}#{visit}")`.

**Agent stations** use the existing `forge.run_agent` and `forge.resume_agent` activities (heartbeat, `maximum_attempts=1`).
- `RunAgentInput` gains optional `station_visit_id`, `role`, `skill_profile`, rendered `objective`, `context_template`, `model` and `max_tokens`.
- The activity creates the `agent_run` row with `workflow_run_id` set and runs under `BudgetedModelClient(min(station cap, run remaining))`.
- It returns tokens, `cost_usd` (null when unpriced) and the decision.
- The workflow computes `low_confidence`.

**Service stations** use one activity, `forge.run_service_station(name, params, resolved_inputs, idempotency_key)`, which dispatches to the bound body. The retry policy and `start_to_close` come from the station, clamped by the catalog. The idempotency key is `station:{run}:{state}:{visit}`.

**Human stations**
1. `open_station_gate` looks for an `approval_request` with `subject_type='step'` and `subject_id=visit_id`, in any status. Only when none exists does it call `ApprovalService.create`, with `workflow_run_id`, `expires_at = now + sla` and `gate_payload {run, state, visit, summary}`. `uq_pending_gate` alone is not enough here: a retried create after a quick decision would open a second gate.
2. The workflow waits on `wait_condition(hint or cancel, timeout=min(15 min, deadline - now))`.
3. On every wake, `read_station_gate` reads `approval_request` and `approval_decision`. It checks the workspace, run, state and visit, and requires every decider to be a user principal.
   - `approved`, `rejected` and `changes_requested` become the outcome, and the transition actor is the decider.
   - Pending means keep waiting.
   - Past the deadline and still pending: it expires the gate with a conditional `pending → expired` update (the worker's `_expire_gate`) and returns `expired`.
4. The hint is the Signal `station_gate_changed(approval_request_id)`, sent by `StepAwareGateHook`.
   - The hook wraps the existing hook for `spec`, `plan` and `pr` at the API composition root.
   - It runs the wrapped hook unchanged, so the `pr` attestation still happens. Then, for `subject_type='step'`, it sends the hint.
   - A failed send is logged and never raised, so a Temporal outage cannot block or roll back a decision.
   - The Signal carries nothing the workflow trusts.
5. No new context provider is needed. `ApprovalService.get_context` already falls back to a read-only context built from `gate_payload`.

**Park and resume.**
- On a station run, `POST /workflow/runs/{id}/transition` accepts only `resume` (with an optional `input`) and `cancel`, and forwards the caller as the actor. Any other event returns 409 `station_run_not_event_driven`.
- The resume Update validator accepts only a user actor. Agents (whose role has no WRITE), automations and system principals cannot un-park a run.
- Resume re-enters the parked station at visit + 1. For `awaiting_input`, it calls `resume_agent` with the input.

**Cancel.** The Signal `cancel_run` does four things: it cancels the in-flight activity (through its heartbeat), expires any open gate with reason `run_cancelled`, runs `cleanup_worktree`, and moves the run to `cancelled`. There is no compensation in v1: a draft PR the run opened stays open.

**Fan-out.** There is none inside a run. Parallel tickets are parallel runs, one per task under the existing single-active-run-per-task rule, capped by `max_active_runs`.

**Versioning.** A run carries its revision's text, so a newer publish never reaches it. Interpreter changes that alter command order use `workflow.patched`. Each shipped line has a replay fixture in `tests/temporal/test_station_replay.py`.

### 4.3 Persistence

One migration, slug `workflow_lines` (the next free number, ADR-0009):
1. `workflow_run.current_state`: drop the enum CHECK, make the column `VARCHAR(64)`, and change the ORM type to `str`. The DTO is already `str`. The downgrade restores the CHECK and fails if any off-enum rows exist.
2. The `workflow_definition.source` CHECK gains `marketplace`.
3. The `marketplace_installation` unique key becomes `(workspace_id, registry_slug, kind, listing_slug)`, matching `marketplace_listing`'s `(registry_id, kind, slug)`. The new key is looser, so existing rows already satisfy it.
4. A new table, `workflow_station_visit` (`WorkspaceScopedModel`):
   - Identity: `id` (the visit id), `workflow_run_id` (FK, cascade), `definition_revision_id` (plain UUID), `state VARCHAR(64)`, `visit INT`, `kind` (`agent | service | human`).
   - Result: `status` (`running | waiting | succeeded | failed | parked | cancelled`), `outcome VARCHAR(48)`, `outputs JSON` (redacted, at most 16 KiB), `error_code`, `error_message` (redacted).
   - Links: `service_name`, `agent_run_id` and `approval_request_id` (plain UUIDs).
   - Cost: `tokens_used`, `cost_usd NUMERIC(12,4) NULL`, `priced BOOL`.
   - Times: `started_at`, `finished_at`.
   - Constraints: `UNIQUE(workflow_run_id, state, visit)`, and an index on `(workspace_id, workflow_run_id)`.
   - It is written only by `forge.persist_station`, an idempotent upsert whose status only moves forward.

`approval_request`, `agent_run` and `workflow_definition_revision` do not change.

Code that the open states touch:
- `store.py:131` and `store.py:189` drop the `DbWorkflowState(...)` coercion.
- `TransitionRecord.from_state` and `to_state` become `str`, and the record gains `run_status: str | None`. `persist_transition` uses `str(...)`.
- `TemporalWorkflowEngine.atransition` returns `str` for station runs.
- `FeatureWorkflow` and `engine.py` are unchanged.

`workflow_run.context` mirrors the latest visit's outputs (`outputs[state]`), the spend (`spend {tokens, usd, unpriced_tokens}`) and `parked`.

**Prerequisites.** Nothing here works without these:
- The API's Temporal engine uses the Postgres projection instead of the in-process store (`routers/workflow.py:120-128`). Otherwise the worker's `persist_transition` retries forever on a missing row (`workflows.py:80`).
- Run ownership comes from `workflow_run.workspace_id`. The in-process `WorkflowOwnership` map loses every run on restart.
- Both engines write `workflow_run.definition_revision_id` at start. Today it is only in `context`.

**Read path.** `GET /workflow/runs/{id}/stations` (READ role; a cross-workspace id returns 404) reads `workflow_station_visit`. Cost per merged PR is `SUM(cost_usd)` per run, joined on the `forge/run-<hex>` merge webhook.

**Editor.** `StateNode.kind` gains `agent | service | human` and a `station` payload. For v2 the kind is derived from `stations`, so graph ↔ YAML round-trips without loss. For v2, the catalog offers the service catalog, the three gates and the context templates.

### 4.4 The `workflow_template` installer

**Artifact** (`extra="forbid"`): `{name, title, description?, dsl_yaml}`. `dsl_yaml` is at most 128 KiB and is stored verbatim as the revision's `dsl_yaml`. `name` must equal the DSL's `workflow`. What the line uses (services, profiles, gates, budget) is computed by validation for the preview, never declared in the package.

**`WorkflowTemplateInstaller`** lives in `forge_marketplace/installer.py` and is registered in `INSTALLERS` with `target_kind = "workflow_definition"`, so `build_package` and in-app publish validate through it too. `validate` is declarative: it never evals, execs or imports package content.
1. Validate the strict artifact model.
2. Scan for secrets: the approval-sdk redaction patterns over `dsl_yaml`, and a key-name scan over `params`. A hit raises `SchemaInvalid("secret_like_content")`.
3. `parse_definition` must return a `LineDefinition`.
4. Run `validate_line`. Any error raises `SchemaInvalid` listing the codes.
5. `default_feature` and `incident` are reserved names: `SchemaInvalid("reserved_name")`.
6. Return:
   - `resolved_config {name, title, description, dsl_yaml, summary {agents, services, human_gates, budget}}`;
   - `requires_admin_followup`: each service's credential, each skill profile, "requires the temporal engine", and "bind services on the worker".

**Verification for every kind.** A pure function, `verify_package(manifest, entry, version, request, registry_key)`, runs these checks in order:
1. `manifest.kind == entry.kind == request.kind`, the same for slug, and `manifest.version == version.version`.
2. `compute_manifest_hash(manifest) == version.manifest_hash`. Today the signature covers `manifest_hash`, but nothing ties the fetched manifest to it, so a genuine signature can sit beside swapped content.
3. `manifest.content_hash == version.content_hash`.
4. The existing `verify_version`.

A failure in checks 1–3 is a hard block, `manifest_mismatch`.

**Kind policy.**
- `workflow_template` installs and updates only with status `verified`, at every registry trust level.
- `acknowledge_unverified` is ignored for this kind, including on update. Today `svc:1094` forces it on.
- Yanked versions are refused.

**Service changes** (`marketplace_service.py`)
- **Dispatch.** Apply and update dispatch explicitly by kind, and uninstall by `target_kind`. The "else means MCP" branches at `svc:872-877`, `svc:1099-1112` and `svc:1150-1153` are removed. `UnknownArtifactKind` maps to 422 `unknown_kind` with a committed denied audit row, in `build_install_plan`, in `publish_listing` and in the router.
- **Install** runs in one session with flush only, using `DbWorkflowDefinitionRepository` on the marketplace session. It never uses `WorkflowEditorService`, which commits internally.
  - Same installation, version and `content_hash`: no-op, audited as `noop`. Another version: routed to update.
  - The target name is `override_name` or `name`. If a definition with that name exists and is not this installation's target: 409 `name_conflict`, audited.
  - Steps, in order:
    1. `create_definition(source=marketplace)`, or `set_active(True)` on a reinstall.
    2. `create_draft(dsl_yaml, graph_json, dsl_version="2", notes="<registry>/<slug>@<version> <content_hash>")`. `graph_json` is computed here with `yaml_to_graph` and `auto_layout`, not in the SDK.
    3. Mark the draft valid, then `publish_draft`.
    4. `_upsert_installation(target_kind="workflow_definition", target_object_id=definition.id)`.
    5. Audit: the marketplace log, plus F39 `marketplace.install` and `workflow_definition.publish`.
    6. One commit.
  - A failure before the commit leaves only the denied audit row. An `IntegrityError` on commit returns 409 `concurrent_install`.
- **Update**
  - A pinned installation returns 409 `installation_pinned`.
  - Re-fetch, re-verify and re-validate. The same hash is a no-op.
  - An open draft returns 409.
  - Otherwise append a revision and publish it.
  - Rolling back means naming an older version explicitly. That also appends a new revision.
  - Revisions are never mutated, and in-flight runs keep their revision.
- **Uninstall**
  - `set_active(False)` archives the definition. Revisions are kept, and `load_pinned` still loads them for in-flight runs.
  - Status becomes `uninstalled`. Keep `target_object_id`.
  - Audit `marketplace.uninstall` and `workflow_definition.archive`.
  - New starts with that name return 404.
- **Editor.** `_require_editable` (`editor/service.py:484-490`) also refuses `source=marketplace`, with 409 `definition_managed_by_marketplace`, so installed content keeps matching its `content_hash`. Forking is allowed. A fork becomes a `custom` v2 definition, and `validate_line` still guards it.
- **Audit** also covers changes to a registry's key or trust level, and `set_pin`, because the registry key is now the trust root for lines.
- **Access.** Install, update and uninstall stay ADMIN only. Every query filters on `workspace_id`, and a cross-workspace id returns 404.

**Signing.** `forge_marketplace/signing.py` adds `sign_manifest_hash(manifest_hash, private_key)`, and the CLI adds `forge marketplace sign --key-file`. Both make an Ed25519 signature over the UTF-8 `manifest_hash`, which matches `verifier.py`. In-app publish stores `signature=None`, so it can never produce an installable line.

### 4.5 First line: implementation loop

It ships as a signed `workflow_template` with this `dsl_yaml`:

```yaml
workflow: implementation_loop
version: "1"
dsl_version: "2"
initial: prepare
terminals: {done_proposed: succeeded, ready_for_review: succeeded}
budget: {max_tokens: 3000000, max_usd: 60}
concurrency: {max_active_runs: 3}
stations:
  prepare:       {kind: service, service: forge.prepare_worktree, inputs: {repo: task.repo}}
  assess:
    kind: agent
    role: researcher
    skill_profile: implementer
    context_template: implementation
    objective: "Decide whether {{task.title}} is already satisfied on main. Cite files and tests."
    decisions: [already_done, needs_work]
    confidence_threshold: 0.8
    budget: {max_tokens: 150000}
  propose_done:  {kind: service, service: forge.tracker_comment, inputs: {ticket: trigger.external_ref, body: outputs.assess.summary_ref}}
  define_done:
    kind: agent
    role: tester
    skill_profile: implementer
    context_template: implementation
    objective: "Write a brief and failing tests that define done for {{task.title}}. No production code."
    budget: {max_tokens: 400000}
  confirm_red:   {kind: service, service: forge.confirm_red}
  open_draft_pr: {kind: service, service: forge.open_pr, inputs: {task: task.id}, params: {draft: true}}
  implement:
    kind: agent
    role: implementer
    skill_profile: implementer
    context_template: implementation
    objective: "Make the failing tests pass. Last failure: {{outputs.local_checks.failure_summary}} {{outputs.await_ci.failure_summary}}"
    max_visits: 6
    budget: {max_tokens: 600000}
  local_checks:  {kind: service, service: forge.run_checks}
  push:          {kind: service, service: forge.push_branch, inputs: {pr: outputs.open_draft_pr.pr_ref}, retry: {max_attempts: 3}}
  await_ci:      {kind: service, service: forge.await_ci, inputs: {pr: outputs.open_draft_pr.pr_ref}, timeout_seconds: 3600}
  mark_ready:    {kind: service, service: forge.mark_pr_ready, inputs: {pr: outputs.open_draft_pr.pr_ref}}
transitions:
  - {from: prepare,       to: assess,           when: succeeded}
  - {from: assess,        to: propose_done,     when: already_done}
  - {from: assess,        to: define_done,      when: needs_work}
  - {from: propose_done,  to: done_proposed,    when: succeeded}
  - {from: define_done,   to: confirm_red,      when: succeeded}
  - {from: confirm_red,   to: open_draft_pr,    when: succeeded}
  - {from: confirm_red,   to: define_done,      when: not_red}
  - {from: open_draft_pr, to: implement,        when: succeeded}
  - {from: implement,     to: local_checks,     when: succeeded}
  - {from: local_checks,  to: push,             when: succeeded}
  - {from: local_checks,  to: implement,        when: checks_failed}
  - {from: push,          to: await_ci,         when: succeeded}
  - {from: await_ci,      to: mark_ready,       when: succeeded}
  - {from: await_ci,      to: implement,        when: checks_failed}
  - {from: mark_ready,    to: ready_for_review, when: succeeded}
```

- Each K bar is met:
  - Failing tests come first, and a service confirms they fail (`confirm_red`).
  - Lint and tests run before every push (`local_checks`), checked by a service rather than by the agent's own report.
  - The budget is a hard cap.
  - The PR is real (G2).
  - The line never merges.
- The `already_done` path only posts a comment. The tracker owner decides (ADR-0005, ADR-0019).
- Unrouted outcomes take the defaults:
  - `implement` `visit_limit`, `low_confidence` or `awaiting_input`, and `await_ci` `no_checks`: the run parks for a person.
  - `failed`, `timed_out`, `budget_exhausted` or an unbound service: only this ticket's run fails, and other tickets keep running (lore #2370).
- A person approves and merges the PR through the existing `pr` gate. Merge bookkeeping keys on the `forge/run-<hex>` branch (J §2.2).

### 4.6 Delivery

About 6–7 engineer-weeks, not counting the service bodies.
- **P0, about 1 week.** The prerequisites in §4.3, plus `verify_package`, explicit dispatch, `unknown_kind` returning 422, the `workflow_template` kind policy, and signing.
- **P1, about 1.5 weeks.** `forge_contracts/lines.py`, `validate_line`, the installer and the migration. Lines can be installed and validated. Starting one returns 503 `durable_engine_required` until P2.
- **P2, about 2.5 weeks.**
  - `StationWorkflow` and its activities.
  - `StepAwareGateHook`.
  - Start with a definition, and park, resume and cancel.
  - Temporal tests for each station kind: a lost hint, a forged Signal, budget exhaustion, `visit_limit`, and replay for `FeatureWorkflow` unchanged.
- **P3, 1–2 weeks.** Bind the phase-1 service bodies, sign and publish `implementation_loop`, and dogfood it against the 43% yield and roughly $105-per-merged-PR bars.

Not in this spec:
- dependency-aware ticket scheduling;
- tracker-event triggers;
- lines that start without a task;
- human-station gates other than spec, plan and pr;
- compensation on cancel;
- fan-out inside a run;
- re-running from a chosen node;
- `continue_as_new`.

### 4.7 Decisions on the open questions (2026-11-08)

- **Feature-run gates go through approvals first (P0, before any line work).** Today a member can
  post `spec_approved_by_human`, `plan_approved_by_human` or `review_approved_by_human` straight to
  `POST /workflow/runs/{id}/transition`, so the approval service's rules (required approvals, no
  self-approval, attestation) never apply to feature runs. Entering `spec_review`, `plan_review` or
  `awaiting_review` opens an approval request for the run (`subject_type='workflow_run'`, the
  matching gate); a hook submits the `*_by_human` event, with the decider as actor, once that
  request resolves `approved`; `/transition` refuses the three events with 409
  `gate_events_come_from_approvals` (ADR-0005). The same hook carries K's station hint.
- **Budgets.** `max_tokens` is the enforced cap. `max_usd` is recorded and alerts, but does not
  stop a run, until programme C/D shows the price tables are accurate enough to enforce.
- **Cancel** leaves a draft PR open, with a comment saying the run was cancelled.
- **`postgres_fsm` deployments** can install lines and get 503 `durable_engine_required` when they
  start one. Accepted: lines need durable execution.
- **Registry signing key.** The maintainer of the official registry holds it; rotation is a
  registry-key change, audited (§4.4). Who that maintainer is, is the owner's decision.
- **K2 (next, not in this spec):** dependency-aware scheduling as part of the G1d dispatcher (blocks
  links from each tracker; settled = merged PR or closed issue); lines without a task by making
  `workflow_run.task_id` nullable for line runs (no synthetic tasks); one generic `station` gate in
  ADR-0005 lockstep for triage and onboarding hand-offs; `continue_as_new` for long human SLAs.
- **To verify in P2:** an exception in an `ApprovalService.resolve` hook must not roll back the
  recorded decision (the wrapped `pr` attestation hook can raise).

### 4.8 Known gaps after P0 (2026-11-08)

Found by the P0 verification; none is a regression, and each has an owner phase.
- **A rejected plan or pr gate leaves a `default_feature` run at its gate.** The definition has no
  edge out of `plan_review` or `awaiting_review` other than approval, so after a rejection the run
  can only be cancelled. P2 decides the route (an edge back to drafting, or reopening the gate),
  alongside the station routing for lines.
- **Feature-run gates need one approval.** Both engines open them with `required_approvals = 1` and
  no `review_rules` in the payload, so repo policy cannot raise the quorum yet. P2 stamps the
  repo's resolved review rules when the engines open a gate.
- **The V1 engine cannot pass the pr merge guard.** Nothing in V1 produces `ci_status_green` or
  `spec_validated`, so an approved pr gate records `workflow_event.status = failed` and the run
  waits for sync. This predates P0. Feature runs that must merge use the Temporal engine.
- **Temporal start launches the workflow before the API commits the run row.** If the commit fails
  after `start_workflow`, the workflow retries `persist_transition` on a row that never landed.
  P2 commits the row first and terminates the workflow when the commit fails.
- **A raising `pr` attestation hook returns 500 after the decision is recorded.** Already listed
  under "To verify in P2".

### 4.9 Carried forward from P1 (2026-11-08)

- **There was no CHECK to drop.** At 0044, `workflow_run.current_state` and
  `workflow_definition.source` were plain `VARCHAR` with no database CHECK (`enum_type` emits
  none). 0045 therefore drops and restores nothing; the downgrade refuses rows the older code
  cannot load instead. Real enum CHECKs across the schema would be a separate change.
- **`workflow_station_visit` nullability** (§4.3 is silent): identity columns, `kind`, `status`
  and `started_at` are NOT NULL; `outputs` defaults to `{}`, `tokens_used` to 0, `priced` to
  false; result, link, cost and `finished_at` columns are nullable.
- **For P1c.** Installation lookups by `(workspace_id, registry_slug, listing_slug)` must add
  `kind` now that two kinds may share a slug. The editor's `DefinitionOrigin` must accept
  `marketplace`. The editor writes a revision's `dsl_version` from the workflow's own `version`;
  v2 revisions must store `"2"`.
- **For P2.** `TemporalWorkflowEngine.atransition` returns `str` for station runs (§4.3), and
  `/transition` refuses everything but `resume` and `cancel` on them (§4.2).

### 4.10 Installer decisions (2026-11-08)

- **Secret scan.** The API and the offline signer use one detector, the marketplace SDK's
  token-shape detector (private-key blocks, GitHub, AWS, Slack, `sk-` keys, JWTs), over the raw
  `dsl_yaml`, every decoded string and key, and the title and description. The observability
  redactor is not used: its header patterns refuse ordinary text such as "Write basic tests", and
  a package must never sign cleanly and then fail every install.
- **Names.** A line's definition name is its DSL `workflow` name and follows the editor rule
  `^[a-z][a-z0-9_]{1,62}$`. `override_name` is refused for lines (422 `override_not_supported`).
  A line renamed upstream is refused (422 `name_mismatch`); a registry publishes a renamed line
  under a new slug. Bundled names are reserved.
- **Concurrency.** Install, update and uninstall of one line installation run one at a time (row
  lock). Uninstall clears the pin.
- **Audit names.** Publishing and archiving an installed line reuse the editor's
  `workflow_definition.published` and `workflow_definition.archived` events, so one query finds
  every publish and archive.

## 5. Out of scope

Running lore, its `floor` engine or its planning-editor packages (unlicensed; ADR-0010). Kubernetes `Agent` CRDs.
