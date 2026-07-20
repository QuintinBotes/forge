# Trust layer

Four features that make autonomous change *verifiable* rather than merely
recorded. Where the [run-trace viewer](./concepts.md) answers *what did the
agent do*, the trust layer answers *can I prove it, could I replay it, did
anything adversarial catch it, and did this config change quietly make the
system worse*. Each feature is a self-contained, append-only record backed by
one Postgres table; none of them can mutate existing behaviour (every migration
is purely additive).

This document is written from the code. Where a capability is parked or
Phase-A, that is stated plainly in the feature's **Current limitations** — a
parked stand-in that looks like a pass is a trust hazard, so it is named.

For where these sit in the platform, see [Architecture](./architecture.md); for
the mental model, see [Concepts](./concepts.md).

| Feature | One line | Table | Migration |
|---------|----------|-------|-----------|
| [Attested Changesets](#attested-changesets) | DSSE-signed in-toto provenance over a changeset | `attestation` | `0036` |
| [Time-Travel Runs](#time-travel-runs) | Deterministic record-replay (and counterfactual fork) of an agent run | `run_recording` | `0037` |
| [Red-Team Gate](#red-team-gate) | A heterogeneous adversary must fail to break the change before the human gate | `red_team_record` | `0038` |
| [Self-Eval Gate](#self-eval-gate) | Block a config change that regresses a workspace's private per-repo suite | `benchmark_suite` (+cols), `self_eval_baseline` | `0039`, `0040` |

---

## Attested Changesets

### What it is

An **Attested Changeset** is a cryptographically signed, tamper-evident record
of the *truthful runtime provenance* of the agent run that produced a change —
what actually ran, never a planned or intended value. It is a
[DSSE](https://github.com/secure-systems-lab/dsse) (Dead Simple Signing
Envelope) wrapping an [in-toto v1 Statement](https://github.com/in-toto/attestation),
whose predicate (`ChangesetProvenance`) records the agent role and model, the
model version the provider reported, the spec revision the prompt was built
against, the sandbox isolation tier the commands executed under, a content hash
of the policy in effect, the ordered tool names invoked (names only — no
arguments, since an attestation may be exported outside the trust boundary the
raw step log lives in), the human approver, the linked workflow/agent run, the
PR numbers, and the spec key/version.

The contract lives in
`packages/contracts/forge_contracts/attestation.py`. The signer/verifier
(`packages/observability/forge_obs/attest/signing.py`) uses raw **Ed25519** keys
and signs over the DSSE **PAE** (Pre-Authentication Encoding) of
`(payloadType, payload)`, so relabeling the payload type invalidates the
signature. Signing and verifying are pure and total — malformed input is "not
verified", never an exception.

The signing key comes from `FORGE_ATTEST_SIGNING_KEY` (a base64 32-byte Ed25519
seed). Signing an attestation is a provenance convenience, not a confidentiality
boundary, so an **unset** key fails *open* to a loudly-warned, process-ephemeral
key (a real deployment must set a stable key or verification fails after a
restart); a key that *is* set but malformed fails *closed*. The `keyid` is the
sha256 of the raw public key bytes — a content-derived id a verifier looks up
rather than trusting a signer-asserted one.

### Data model

`AttestationService` (`apps/api/forge_api/services/attestation_service.py`)
reads the strongly-linked rows for a workflow run (`AgentRun`,
`TraceabilityCriterionLink`, `SpecVersion`), assembles the `ChangesetProvenance`
predicate inside an in-toto `Statement`, signs it, inserts one append-only
`Attestation` row, and emits a `changeset.attested` event through the F39 audit
hash chain — whose `detail_ref` points back at the attestation row, and whose
returned `seq` is recorded on the row (`audit_seq`). Because the table is
append-only, the id is pre-generated so the audit event can reference it and
`audit_seq` lands in the single INSERT.

The `attestation` table (migration `0036`,
`packages/db/forge_db/models/attestation.py`) is append-only, hardened by the
shared Postgres BEFORE UPDATE/DELETE immutability trigger. Columns:
`subject_digest`, `predicate_type`, `envelope` (the full DSSE JSON),
`payload_hash` (sha256 of the PAE-encoded payload the signature covers),
`keyid`, nullable `workflow_run_id`/`agent_run_id`, `pr_numbers`, `spec_key`,
`spec_version`, `audit_seq`, and `merkle_leaf_hash` (reserved for a future
batch-Merkle anchoring scheme, unused today).

### API / CLI surface

Attestations are minted as a **side effect of approval**:
`PrAttestationResolutionHook` is registered against the F36 approval system, so
approving a `pr` gate that carries a `workflow_run_id` attests the changeset
(recording the approving user as `human_approver`). There is no dedicated REST
endpoint or UI yet.

Verification is offline-first via the `forge-verify` CLI (`python -m
forge_api.cli_verify`, `apps/api/forge_api/cli_verify.py`). Three
mutually-exclusive modes, all exiting **non-zero on any verification failure** so
they can gate a release:

| Mode | Does | Needs a DB? |
|------|------|-------------|
| `--attestation <file\|->` | Offline DSSE-verify an envelope's Ed25519 signature against `--public-key` (or the public half of `FORGE_ATTEST_SIGNING_KEY`) | No |
| `--run <id>` | Load the stored `Attestation`, re-derive `payload_hash` from the envelope's PAE and confirm it matches the recorded column, then verify the signature | Yes |
| `--audit-export <ndjson>` | Re-walk an F39 audit-chain export offline: recompute each row's `payload_hash`/`entry_hash` and re-check the per-workspace `prev_hash` linkage | No |

Exit codes: `0` verified / chain intact, `1` rejected or tampered, `3` no
database configured (a "can't check" distinguished from a "checked and it's
tampered").

### Current limitations

- **Minted only as an approval side effect.** An attestation is produced when a
  `pr` approval gate carrying a `workflow_run_id` is approved. There is **no
  REST endpoint and no UI** to list, fetch, or trigger attestations — those
  arrive in a later task (Attested Changesets REST endpoints + UI, Task 19).
- **Default signing key is process-ephemeral.** If `FORGE_ATTEST_SIGNING_KEY`
  is unset, signatures are made with a warned-about ephemeral key and will fail
  verification after a restart or from another process. Set a stable key in any
  real deployment.
- `merkle_leaf_hash` is reserved and unused; there is no batch-Merkle-root
  anchoring yet.

---

## Time-Travel Runs

### What it is

**Time-Travel Runs** is deterministic record-replay of an agent run. The two
nondeterministic boundaries — LLM completions and tool calls — are recorded into
a redacted **cassette** (`RunCassette`) keyed by call-order. Replay does **not**
re-seed the model (the target providers 400 on `seed`/`temperature`); it
substitutes the recorded response/result back in by call-index, so no live model
or tool is ever touched during a replay. A per-call divergence canary reports
the first point where a re-run stops matching the tape.

Recording is opt-in behind `FORGE_RECORD_RUNS=1` (default **off**). When
enabled, `build_agent_runner`
(`apps/worker/forge_worker/agent_runner.py`) wraps the model and tools with
`RecordingModelClient` / `RecordingToolRegistry` bound to a fresh cassette;
`persist_run_recording` then redacts the cassette, offloads oversized tool
outputs to the artifact store, and inserts an append-only `RunRecording` row.

### Data model

The `run_recording` table (migration `0037`,
`packages/db/forge_db/models/run_recording.py`) is append-only via the shared
immutability trigger. Columns: nullable `agent_run_id`/`workflow_run_id`, the
redacted `cassette` snapshot (LLM calls, tool calls, and the redacted env, keyed
by call-index), `model` (the id the run was driven under — nullable, since a
tool-only run has no model to report), and `content_hash` (sha256 of the
cassette's canonical JSON — a whole-tape fingerprint).

### API / CLI surface

Two endpoints on the agent router (`apps/api/forge_api/routers/agent.py`), both
loading the workspace-scoped recording (404 for a missing or foreign-tenant
one):

| Endpoint | Permission | Behaviour |
|----------|-----------|-----------|
| `POST /agent/runs/{run_id}/replay` | `READ` | Re-run the objective by substitution and return a per-step diff plus any divergence. Read-only — never mutates the recording or produces a new persisted run. |
| `POST /agent/runs/{run_id}/fork` | `RUN_AGENT` | Counterfactual fork: replay up to `fork_index`, then from the fork on, LLM completions run against a **different** model (`model`, optionally with a `prompt_override` appended to the system prompt) and tool dispatches run **live**. If the pre-fork prefix no longer reproduces the tape, the fork aborts and the divergence is reported. |

The `forge-replay` CLI (`python -m forge_api.cli_replay`,
`apps/api/forge_api/cli_replay.py`) loads a persisted cassette by id, replays a
supplied objective by substitution, and prints a step-by-step diff. Exit codes:
`0` reproduced, `1` diverged or error, `3` no database configured.

A web surface exists at
`apps/web/src/components/run-trace/time-travel-replay.tsx`.

### Current limitations

- **Recording is opt-in and off by default** (`FORGE_RECORD_RUNS=1`). A run not
  recorded has no cassette to replay or fork.
- Replay is **by call-index substitution**, not model re-seeding — a structural
  change to the run (different tool order) diverges rather than silently
  re-running.
- A tool-only run (zero LLM calls) records `model=None`.

---

## Red-Team Gate

### What it is

The **Red-Team Gate** puts a distinct **adversary** agent between a candidate
change and the human implementation gate. The adversary runs a **heterogeneous**
model — a different provider than the coder used — and is scoped to the
`ADVERSARY` role tools: it may read the repo, author a candidate failing test,
and run SAST, but it can **never edit product code**. It attacks the candidate
diff, and a change is **BLOCKED** only when:

- the adversary's authored test **actually fails when executed in a sandbox**
  (non-zero exit) — an *executed* failing test, never the adversary's own
  self-reported pass; or
- the adversary reports a structured spec-violation that references a **real**
  `AcceptanceCriterion` of the spec.

Otherwise the change earns a `survived` verdict — the "survived adversarial
review" signal that feeds the Attested Changeset. Heterogeneity is enforced up
front: a homogeneous adversary (same provider as the coder) is rejected with
`HomogeneousAdversaryError` before any model or sandbox work. The harness lives
in `packages/multi-agent-coordinator/forge_coordinator/red_team.py`
(`run_red_team`).

### Data model

The `red_team_record` table (migration `0038`,
`packages/db/forge_db/models/red_team.py`) is append-only via the shared
immutability trigger. Columns: nullable `workflow_run_id`, `verdict`
(`blocked`/`survived`), `kind` (`failing_test`, `spec_violation`, or `parked`),
`evidence` (the structured attack result), and the heterogeneous
`adversary_model` / `coder_model` pair (both nullable — a parked-pass has no
adversary model).

### API / CLI surface

The gate is driven inside the **Temporal** workflow
(`packages/workflow-engine/forge_workflow/temporal/workflows.py`): after the spec
is drafted and clarified but **before** the human spec-approval gate, the
workflow runs a red-team scan. A `blocked` verdict routes the change back for
changes (→ clarification → spec review) so a human must address the finding; a
`survived` verdict proceeds unchanged and the recorder writes the
`RedTeamRecord` plus a `redteam.survived` audit event.

The verdict is surfaced read-only at `GET /workflow/runs/{run_id}/red-team`
(`apps/api/forge_api/routers/workflow.py`), returning the latest verdict plus the
full scan history (a `blocked` scan followed by a re-submitted `survived` one is
common). It reads the append-only table scoped to the caller's workspace on the
row itself; an unscanned or foreign run reads as `latest=None` with an empty
history — never a 404, so the endpoint never leaks cross-tenant existence.

### Current limitations

- **Parked-pass when no adversary is wired.** The Temporal activity's default
  `red_team_fn`
  (`packages/workflow-engine/forge_workflow/temporal/activities.py`,
  `_default_red_team`) records a **parked-pass**: `verdict=survived`,
  `kind=parked`, `evidence={"parked": true, "reason": "no adversary model/sandbox
  wired"}`. This is a *park-don't-fake* stand-in so the durable spine runs and
  the human gate is still reached — it is **not** a real adversarial review, and
  a `survived`/`parked` record must not be read as "an adversary tried and
  failed to break this". A real deployment must inject a `red_team_fn` that runs
  the heterogeneous, sandboxed adversary (`run_red_team`).
- **Temporal path only.** The scan runs today only inside the Temporal workflow.
  A trigger endpoint and a non-Temporal worker path arrive in a later task
  (Red-Team trigger endpoint + non-Temporal worker path, Task 20).

---

## Self-Eval Gate

### What it is

The **Self-Eval Gate** refuses a model / prompt / router **config change** if,
re-evaluated on a workspace's **private per-repo regression suite**, its
resolution rate drops below a frozen baseline. The private suite is a *living
accumulator* minted from the org's own merged PRs: each merge can mint one hidden
regression case, and those minted test ids live only on disk in the suite dir —
they never enter a model prompt.

The gate itself (`packages/evaluation/forge_eval/sweval/gate.py`,
`SelfEvalGate.check_config`) is small and injectable: it looks up the baseline,
runs the eval runner over the proposed config, and raises
`SelfEvalRegressionError` when the new resolution rate is below the baseline. It
is a **no-op on cold start** (no baseline, or no private suite) so existing
config flows stay green until a suite exists, and a `force=True` override skips
the check (audited).

### Data model

Two migrations:

- **`0039`** extends `benchmark_suite` with three additive columns:
  `workspace_id` (nullable FK), `repo_id` (nullable, free-form), and `private`
  (`NOT NULL`, default `false`). A NULL `workspace_id` / `private=false`
  preserves today's shared/community-suite semantics for every pre-existing row;
  only a newly-minted self-eval suite opts into `private=true`.
- **`0040`** adds the `self_eval_baseline` table
  (`packages/db/forge_db/models/benchmark.py`, `SelfEvalBaseline`): exactly one
  baseline per `(workspace, suite)` (unique constraint), carrying
  `baseline_rate`, `resolved`, `total`, a **redacted** `config` snapshot (no
  secrets ever land in this table), and `recorded_by`. A run upserts the row.

`SelfEvalService`
(`apps/api/forge_api/services/self_eval_service.py`) is the storage layer:
`workspace_baseline` is the gate's baseline lookup (most-recently-updated wins;
`None` on cold start), and `record_baseline` upserts — with `overwrite=False`, an
existing baseline is left untouched so a regressing run can never silently
*lower* the bar it defends.

### API / CLI surface

The gate is consulted at the Adaptive Orchestration config-change API
(`apps/api/forge_api/routers/ao_settings.py`) on `PUT /ao/role-config/{role}`
and `PUT /ao/settings`. Enforcement is behind the `self_eval_enforce` app
setting (a no-op unless set). On a regression the block is **audited**
(`ao.config.self_eval_blocked`, its own committed row) and a `409` is raised
*before* the mutation is applied; a `force=true` override is audited
(`ao.config.self_eval_forced`) and allowed through.

Two worker tasks own the offline, agent-driven halves (they run in the worker,
never a request path, because a real run is minutes-long):

- `forge.self_eval.mint` (`apps/worker/forge_worker/tasks/self_eval_mint.py`) —
  mints a hidden regression case from each merged PR and appends it to the
  private suite, re-freezing the manifest with a fresh `content_hash`.
- `forge.self_eval.run` (`apps/worker/forge_worker/tasks/self_eval_run.py`) —
  drives the `ProductionEvalRunner` over the private suite and records the
  baseline the gate blocks against. It is an honest **no-op** until an operator
  provisions the prerequisites (a published private suite, `FORGE_BENCHMARK_DIR`,
  a local clone under `FORGE_SELF_EVAL_REPO_ROOT/<repo_id>`, and a BYOK model) —
  every missing piece resolves to a `scored: false` reason rather than
  fabricating a score.

### Current limitations

- **Phase A: the API-layer gate no-ops without an injected runner.** The default
  eval runner at the API layer (`_unavailable_runner` in
  `apps/api/forge_api/services/self_eval_gate.py`) returns `None`, because
  running a workspace's private suite is a minutes-long worker job and
  `apps/api` cannot import `forge_worker` without an import cycle. So at
  config-change time the API gate has **no fresh scorecard for the proposed
  config and no-ops** — the gate *mechanism* (baseline lookup, regression block,
  force override, audit) is fully wired and exercised in tests by injecting a
  runner, but a stock API deployment does not evaluate the proposed config
  inline.
- **Enforcement is off by default** (`self_eval_enforce`).
- **Baseline establishment/refresh is worker-driven only.** It is the
  `forge.self_eval.run` Celery task. (Some in-code docstrings refer to this as
  "the worker-owned `POST /ao/self-eval/runs` path" — that HTTP endpoint does
  **not** exist today; `benchmarks.py` states there is no inline `POST /runs`,
  and the baseline run is the Celery task.)
- **No web UI yet** (Self-Eval web UI, Task 21).
