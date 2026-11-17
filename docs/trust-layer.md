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

## The governed pipeline on Temporal

A feature run on the Temporal engine (`FeatureWorkflow`) now runs on real
activities from start to the `pr` gate (#170, split into #250 to #253). The
worker (`apps/worker/scf_worker/temporal_main.py` and `temporal_services.py`)
binds every activity except `forge.resume_agent`:

- **Guard inputs** read the execute preconditions from stored data. A connected
  repository target is always required, and so is a policy that parses, or none.
  A skill profile and a knowledge sync are required once the workspace has
  configured one.
- **The agent** runs the task's objective in a per-run worktree, on one
  `agent_run` row per attempt, and records the run branch and its head commit.
- **The checks** run the repository's declared `policy.commands`, from the
  default branch, on a fresh worktree of that head commit, in a sandbox asked for
  no network (see the limitations below). A repository that declares none fails
  with `no_checks_declared`. Each check is recorded on the run's context under
  `checks` (command id, exit status, commit, start time, duration and a redacted
  tail of at most 400 lines of each output stream), and the agent's next attempt
  sees the failing ones in its prompt, as data. The check stage's time limit is
  `CHECKS_ACTIVITY_TIMEOUT` (30 minutes by default).
- **The independent reviewer** (#173) judges a change whose checks all pass,
  before its pull request. It is an agent run with role `reviewer` on the
  workflow run, with read-only tools (`read_file`, `list_dir`, `search_code`).
  It is given the diff against the base branch and the acceptance criteria as
  untrusted data, cut at 60,000 characters (the verdict then says `truncated`).
  Its repository instructions are the base branch's root `AGENTS.md`, never
  one the change adds or edits.
  - **Its model** is the `reviewer` role's model in the workspace's or project's
    role configuration. The built-in default and a tier name count as not
    configured, and the provider's key comes from the worker's environment
    (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`).
  - **Its family** must differ from the builder's model, which the builder's run
    records; the red-team harness uses the same family comparison
    (`scf_agent.providers.family`).
  - **Its verdict** is the check `independent_review`. `approve` lets the pull
    request open and is named in its body. `request_changes` (also any output
    that does not parse, as `unparseable_verdict`, and a reviewer run that did
    not succeed, as `reviewer_run_failed`) fails the checks, so the
    builder's next attempt sees the findings within the retry budget.
  - **Without a usable reviewer**, the run moves to `needs_human_input` with
    the reason: `reviewer_not_configured`, `reviewer_same_family` or
    `reviewer_family_unknown`.
  - **Each verdict**, its findings and both model ids are on the run's context
    under `reviews`.
- **The red-team adversary** (#283) attacks a change the reviewer approved,
  before its pull request, inside the same `forge.run_checks` activity
  (`apps/worker/scf_worker/red_team.py`). It is given the diff against the base
  branch and the acceptance criteria only as untrusted data, in the same fenced
  block as the reviewer, cut at the same 60,000 characters (the record's
  evidence then says `truncated`). Its repository instructions are the base
  branch's root `AGENTS.md`. Its test runs in the sandbox the declared checks
  get (the workspace's settings strengthened by the default branch's policy, no
  network) on a fresh, detached worktree of the head commit, which is removed
  afterwards, so the test never reaches the run branch.
  - **Its model** is the `adversary` role's model in the role configuration,
    resolved and keyed as for the reviewer, and its family must differ from the
    builder's (`HomogeneousAdversaryError` before any model call).
  - **The off switch** is the `adversary` role's model `off`, in any case (a
    workspace or project override): the run goes on, and the record reads
    `not_run` with the reason `red_team_off`.
  - **Its verdict** is the check `red_team` and a `red_team_record` row at phase
    `pr` with the head commit and both model ids, once per head commit. `blocked`
    fails the checks, so the builder's next attempt sees the failing test's
    output within the retry budget, and a new head commit is scanned again;
    `survived` lets the pull request open.
  - **Without a usable adversary**, the run moves to `needs_human_input` with
    the reason, and the record reads `not_run`: `red_team_not_configured`,
    `red_team_same_family`, or `red_team_failed` (an unknown model family, an
    adversary run that did not finish, or a sandbox fault).
- **The pull request** is opened once: a retry finds the one already opened. A
  branch with no commits is not delivered, and without GitHub App credentials
  the step refuses.
- **The gates** (`spec`, `plan`, `pr`) open approval requests, and only an
  approval moves the run past one. Approving a `pr` request records its
  attestation when the decision is made. When the merge guard refuses the event
  (CI has not reported yet), the request's `gate_payload.workflow_event.status`
  reads `failed` until `POST /workflow/runs/{id}/gates/pr/sync` applies the event
  with the same attestation; then it reads `submitted`.
- **The merge guard reads CI and the spec itself** (#280, FR-011). For each `pr`
  approval, the workflow reads the check runs on the agent's head commit, with a
  token for the one repository that can only read checks. CI is green only when
  there is at least one check run and every one completed as `success`,
  `neutral` or `skipped`; none, a pending one, or any other conclusion is not
  green, and so is CI that cannot be read. The GitHub App needs the **Checks:
  Read-only** permission for this read. `spec_validated` is the spec engine's
  rollup for the task's spec (`passing`: every criterion validated against the
  current spec version), or true for a run with no spec. A
  caller's `ci_status_green` or `spec_validated` is ignored. The run's
  `merge_guard` context records the head commit, the reason (`ci_green`,
  `ci_no_check_runs`, `ci_pending: <names>`, `ci_failed: <names>`,
  `ci_unreadable: <why>`) and where `spec_validated` came from. A refused
  approval is applied later through `gates/pr/sync`, which reads CI again.
- **An issue's `forge:run` label** starts a governed run for the issue's board
  task (creating the task when it has none). The label is not a spec approval
  (owner decision, 2026-11-15, FR-005): the spec gate still opens and waits for a
  person, and the label's sender is recorded as the run's starter on its first
  transition, never as an approver. The agent works from the objective built from
  the re-fetched issue, kept on the run. Tracker write-back for governed runs
  (#278) follows. The backend is read per process: the API, the Celery worker
  and the Temporal worker must all see the same `WORKFLOW_ENGINE_BACKEND` and
  `TEMPORAL_*` settings. A Celery worker without them treats the durable engine
  as not selected: in development it sends issues down the direct, ungoverned
  path, and elsewhere it refuses them.
- **`POST /agent/runs`** (#276) does the same for an objective that names a
  repository: it starts a governed run for the objective's `task_id`, and the
  agent works from the objective's text after the gates. An objective that names
  no repository is still run directly. The 202 names the run (`workflow_run_id`)
  and the agent run its first agent step will create (`run_id`); that agent run
  reads as not found until a person approves the spec and the agent starts. The
  request is refused with 409 when it:
  - has no `task_id` (`governed_run_requires_task`);
  - names a task already on another repository (`task_repository_mismatch`);
  - names a task with an active run (`governed_run_active`);
  - sets anything the governed agent would not apply, such as instructions, a
    model, a skill profile, actions or a second repository
    (`governed_run_unsupported: <fields>`). The task sets those.
- **An ungoverned run** is an agent run with no workflow run: one on the direct
  path, or a supervised multi-agent run (ADR-0007). Owner decision, 2026-11-15
  (spec FR-005): when the durable engine is unavailable, a production deployment
  refuses a repository-changing run; an ungoverned run is allowed only in a
  development environment (`FORGE_ENVIRONMENT=development`), labelled as such
  (#277). So:
  - Outside development, a repository-changing run that would not get a workflow
    run is refused before it runs: `POST /agent/runs` answers 409
    `ungoverned_run_refused: <why>`, an issue's label is skipped with
    `ungoverned_run_refused`, and the worker refuses one already enqueued.
  - When the durable engine is selected but cannot start the run, the request
    is refused (503 `durable_engine_unavailable`, or the issue skipped with that
    reason). SCF never falls back to the direct path.
  - With the durable engine selected, the direct path delivers nothing: a run
    with no workflow run gets `pull_request: {"status": "ungoverned_refused"}`.
  - In development without the durable engine, the direct path runs as before.
    Its pull request body says the run was ungoverned, and every run with no
    workflow run records `governance: none` in its artifacts.
  - Deployment: the production compose file passes `WORKFLOW_ENGINE_BACKEND`
    (default `postgres_fsm`) and the `TEMPORAL_*` settings to the API and the
    Celery worker. Set `WORKFLOW_ENGINE_BACKEND=temporal` and start the
    `temporal` profile to run repository changes; without that, a production
    stack refuses them. The Helm chart renders no Temporal yet, so a Helm
    production install refuses repository-changing runs until it does (#293).
- **A cancel** at a gate expires that gate's open request with the reason
  `run_cancelled`, recorded as whoever cancelled the run. The workflow's own
  cancel path does it again, for a request it opened after the cancel arrived.

### Current limitations

- **The adversary's test runs on the checks' sandbox tier (#184).** On the
  `worktree` tier that is the worker's host, and the command is the one the
  adversary chose.
- **A failed gate submission is not audited (#182).** It is logged and recorded
  on the request (`workflow_event` `failed`), but the audit log records only the
  decision; nothing retries it when CI turns green.
- **A repository with no declared checks spends the agent's retries** before it
  pauses (#272, an owner decision).
- **Some builders cannot be reviewed.** A scripted builder records no model,
  and a local CLI other than Claude's (`local-cli:codex`, for example) names no
  family the reviewer can be shown to differ from, so their runs pause with
  `reviewer_family_unknown` once their checks pass. A `local-cli:claude`
  builder is of Anthropic's family.
- **The review shares the check stage's time limit.** It runs in
  `forge.run_checks` after the declared checks, within `CHECKS_ACTIVITY_TIMEOUT`;
  an attempt that runs out of time runs the checks again.
- **The checks have network on the worktree tier (#184).** Only the container
  tiers enforce the no-network sandbox; the default `worktree` tier runs the
  commands on the worker's host with its network and environment.
- **Held-out scenarios are stored but not run yet (#286).** See below.

### Held-out acceptance scenarios (#176)

A task can carry executable acceptance scenarios that the builder never sees:
files to overlay on the change's checkout and a command to run there. They are
stored in `held_out_scenario` (migration `0049`). No agent-facing surface reads
that table: not the context assembler, the MCP tools, the agent-run routes or
the builder's objective.

- **Who may manage them:** `POST`, `GET` and `DELETE
  /tasks/{task_id}/held-out-scenarios` need write access, so a reader gets `403`
  and a task in another workspace reads as `404`.
- **Drafts:** a scenario is created as a draft, recording the identity that
  drafted it. A file path that could leave the checkout is refused with `422`:
  an absolute path, a `..` segment, or a `.git` segment.
- **Approval:** `POST .../{id}/approve` is a person's decision, recorded with who
  and when. A credential that is not a person (an agent, or an API key that does
  not decide approvals) is refused, and so is the user who drafted the scenario,
  through any credential.
- **Audit:** creating, approving and deleting a scenario are chained into the
  audit log with who did it, never the files or the command.
- **Who drafts them (#258):** per the owner decision of 2026-11-15 (spec FR-022),
  a verifier agent drafts scenarios from the spec before the first `spec`
  approval opens (`forge.draft_held_out_scenarios`, behind the patch marker
  `held-out-drafting-258`, so recorded histories replay unchanged).
  - **Its model:** the reviewer role's configured model, which must be of
    another family than the builder's. A same-family model is refused before any
    model call (`verifier_same_family`); with none configured the run records
    `held_out: verifier_not_configured` and drafts nothing.
  - **What it sees:** the spec (the issue's or the task's text and its
    acceptance criteria) as untrusted data, and no tools.
  - **What is stored:** each scenario is checked against the store's rules and
    stored as a draft, drafted by `agent:<verifier run>/<model>`, with its own
    `held_out.created` audit entry. A scenario the store refuses is dropped with
    its reason.
  - **Why nothing was drafted:** the run's `held_out` records the reason, and the
    spec gate opens as before. The reasons:
    - `verifier_not_configured`: no reviewer model is configured, or there is no
      key for its provider;
    - `verifier_same_family`: the reviewer model is of the builder's family;
    - `verifier_family_unknown`: the builder's family cannot be shown to differ;
    - `verifier_run_failed`: the verifier's run failed or escalated, a provider
      error included;
    - `unparseable_scenarios`: its output did not parse;
    - `no_scenarios_drafted`: its output parsed, but nothing could be stored;
    - `no_spec_to_draft_from`: the spec has neither a description nor
      acceptance criteria.
  - **What is kept out of records:** scenario content. The verifier's agent run
    keeps only ids and titles, with no steps and no events; the workflow run
    keeps only ids and counts; the audit entry keeps only counts and the model.
- **Who approves them (#288):** the spec gate naming the drafted scenarios,
  refusing approval while one is undecided, and withdrawing them when the spec
  changes, is still to come. Until then, scenarios are approved through
  `POST .../approve` above.
- **Running them (#286):** running the approved scenarios against the change
  after the checks pass, and returning only a count of failures to the builder,
  is still to come.

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
`packages/contracts/scf_contracts/attestation.py`. The signer/verifier
(`packages/observability/scf_obs/attest/signing.py`) uses raw **Ed25519** keys
and signs over the DSSE **PAE** (Pre-Authentication Encoding) of
`(payloadType, payload)`, so relabeling the payload type invalidates the
signature. Signing and verifying are pure and total — malformed input is "not
verified", never an exception.

**The signing key (#175).** `FORGE_ATTEST_SIGNING_KEY` (a base64 32-byte Ed25519
seed) wins when it is set, and a set but malformed key fails *closed*. When it is
unset, the API and the worker use the deployment's **stored key**
(`apps/api/scf_api/services/attestation_keys.py`):

- **Where it lives:** the one-row table `attestation_signing_key` (migration
  `0048`). The private seed is encrypted under a subkey of the instance master
  key `FORGE_SECRET_KEY` and is never stored in the clear.
- **How it is made:** the first process that signs generates the key and inserts
  it. A process that loses the insert race reads the winner's key, so the API,
  the worker and a restarted process all sign and verify with one key.
- **Who needs the master key:** every process that signs, so the API and the
  Temporal worker both need `FORGE_SECRET_KEY` (the compose file passes it to
  both) unless `FORGE_ATTEST_SIGNING_KEY` is set. Verifying reads only the public
  key, which is stored in the clear: `scf-verify` needs no master key.
- **If it cannot be read:** a stored key that cannot be decrypted with the
  current `FORGE_SECRET_KEY` or a retained `FORGE_SECRET_KEY_V<n>` fails closed
  instead of being replaced.
- **Development:** with no master key, a process-ephemeral key (one per process,
  nothing stored) is used only under the explicit `FORGE_DEV_INSECURE` opt-in.

The `keyid` is the sha256 of the raw public key bytes: a content-derived id that
a verifier looks up, rather than trusting one the signer asserts.

**Rotating the key.** Set `FORGE_ATTEST_SIGNING_KEY` to a new seed on every
process: it takes precedence over the stored key, which is kept. Verification
uses the current key, so keep the old public key for the attestations it signed
and verify those with `scf-verify --public-key`.

**Rotating the master key.** The stored key follows the KEK rotation drill
(`docs/self-hosting/security.md`). While the old master key is kept as
`FORGE_SECRET_KEY_V<n>`, the stored key is read with it and re-encrypted under the
new `FORGE_SECRET_KEY`. Retiring the old key afterwards is safe once each process
has signed once. Replacing `FORGE_SECRET_KEY` without keeping the old version
makes the stored key unreadable: set `FORGE_ATTEST_SIGNING_KEY` instead.

**The commit and what was verified (#175).** A pull-request attestation's
statement has two subjects:

- the workflow run, with the sha256 of the predicate;
- the repository, with the head commit's `gitCommit` digest.

Its predicate adds the `repository`, the `head_commit` and a `verification`
block:

- **`checks`:** each declared check's id and outcome, with a `checks_verdict`;
- **`ci`:** `not_run` until CI is read back (#280);
- **`reviewer`:** the independent reviewer's verdict and model;
- **`red_team`:** the verdict and model of a scan at the `pr` phase.

A stage that did not run reads `not_run`: never omitted, never a pass.
Attestations made before #175 have none of these fields and still verify.

### Data model

`AttestationService` (`apps/api/scf_api/services/attestation_service.py`)
reads the strongly-linked rows for a workflow run (`AgentRun`,
`TraceabilityCriterionLink`, `SpecVersion`), assembles the `ChangesetProvenance`
predicate inside an in-toto `Statement`, signs it, inserts one append-only
`Attestation` row, and emits a `changeset.attested` event through the F39 audit
hash chain — whose `detail_ref` points back at the attestation row, and whose
returned `seq` is recorded on the row (`audit_seq`). Because the table is
append-only, the id is pre-generated so the audit event can reference it and
`audit_seq` lands in the single INSERT.

The `attestation` table (migration `0036`,
`packages/db/scf_db/models/attestation.py`) is append-only, hardened by the
shared Postgres BEFORE UPDATE/DELETE immutability trigger. Columns:
`subject_digest`, `predicate_type`, `envelope` (the full DSSE JSON),
`payload_hash` (sha256 of the PAE-encoded payload the signature covers),
`keyid`, nullable `workflow_run_id`/`agent_run_id`, `pr_numbers`, `spec_key`,
`spec_version`, `audit_seq`, and `merkle_leaf_hash` (reserved for a future
batch-Merkle anchoring scheme, unused today).

### API / CLI surface

Attestations are minted at two points, and no HTTP route mints one:

- **When a governed run opens its pull request.** `forge.open_pr_with_spec_traceability`
  signs one for the run's head commit before the pull request exists, once per
  run and head commit (a retry finds it). The pull request description names its
  id, payload digest and key id, and the command that verifies that attestation:
  `scf-verify --run <run> --attestation-id <id>`.
- **When the `pr` gate is approved.** `PrAttestationResolutionHook` is
  registered against the F36 approval system, so approving a `pr` gate that
  carries a `workflow_run_id` records a further attestation naming the approving
  user as `human_approver`.

Records are surfaced **read-only over REST**
(`apps/api/scf_api/routers/attestations.py`), workspace-scoped with the same
auth/tenancy contract as the approvals router (cross-workspace ids read as
`404` — no existence leak):

| Endpoint | Returns |
|----------|---------|
| `GET /attestations` | One workspace-scoped page (`limit`/`offset`, newest first; filters: `workflow_run_id`, `agent_run_id`, `spec_key`) |
| `GET /attestations/{id}` | One record (`404` for unknown or foreign ids) |
| `GET /approvals/{id}/attestation` | The gate's linked run's newest attestation that names an approver, else its pull-request attestation (`404` when the run has none) |

Every response carries a computed `verified: bool` produced by the **same
verification seam `scf-verify --run` uses**
(`scf_api.cli_verify.verify_stored_attestation`): re-derive `payload_hash`
from the envelope's PAE encoding, confirm it matches the recorded column, then
Ed25519-verify the signature against the deployment's verification key (the
public half of its signing key, configured or stored). A record this deployment cannot
vouch for honestly reads `verified: false` — the flag is computed per request,
never stored.

In the web app, the approvals review surface renders an attestation panel
(`apps/web/src/components/attestations/attestation-panel.tsx`, on the gate's
run-trace section next to the red-team badge) with three honest states:
signed + verified, signed + verification-failed, and confirmed-absent ("not
attested"). A failed fetch renders nothing: an error is not proof of absence.

Verification is offline-first via the `scf-verify` CLI (`python -m
scf_api.cli_verify`, `apps/api/scf_api/cli_verify.py`). Three
mutually-exclusive modes, all exiting **non-zero on any verification failure** so
they can gate a release:

| Mode | Does | Needs a DB? |
|------|------|-------------|
| `--attestation <file\|->` | Offline DSSE-verify an envelope's Ed25519 signature against `--public-key` (or the public half of `FORGE_ATTEST_SIGNING_KEY`; without either it exits `3`) | No |
| `--run <id>` | Load the run's newest `Attestation` (or the one `--attestation-id` names), re-derive `payload_hash` from the envelope's PAE and confirm it matches the recorded column, then verify the signature against `--public-key` or the public half of the deployment's signing key (configured or stored) | Yes |
| `--audit-export <ndjson>` | Re-walk an F39 audit-chain export offline: recompute each row's `payload_hash`/`entry_hash` and re-check the per-workspace `prev_hash` linkage | No |

Exit codes: `0` verified / chain intact, `1` rejected or tampered, `3` no
database or no key to verify against (a "can't check" distinguished from a
"checked and it's tampered").

### Current limitations

- **Minted only by the pipeline.** The REST surface and the approvals-UI panel
  are **read-only**: there is deliberately no endpoint to trigger or mint an
  attestation.
- **REST `verified` is deployment-relative.** The endpoints verify against the
  public half of this deployment's current signing key, and the raw DSSE
  envelope is not exposed over REST — independent third-party verification goes
  through `scf-verify`, which re-reads the stored row. After a rotation, older
  attestations read `verified: false` here and verify with `--public-key`.
- **CI reads `not_run` (#280)** in a pull-request attestation until CI is read
  back. The red-team scan reads the latest `pr` scan's verdict: `not_run` when
  the workspace turned the scan off.
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
(`apps/worker/scf_worker/agent_runner.py`) wraps the model and tools with
`RecordingModelClient` / `RecordingToolRegistry` bound to a fresh cassette;
`persist_run_recording` then redacts the cassette, offloads oversized tool
outputs to the artifact store, and inserts an append-only `RunRecording` row.

### Data model

The `run_recording` table (migration `0037`,
`packages/db/scf_db/models/run_recording.py`) is append-only via the shared
immutability trigger. Columns: nullable `agent_run_id`/`workflow_run_id`, the
redacted `cassette` snapshot (LLM calls, tool calls, and the redacted env, keyed
by call-index), `model` (the id the run was driven under — nullable, since a
tool-only run has no model to report), and `content_hash` (sha256 of the
cassette's canonical JSON — a whole-tape fingerprint).

### API / CLI surface

Two endpoints on the agent router (`apps/api/scf_api/routers/agent.py`), both
loading the workspace-scoped recording (404 for a missing or foreign-tenant
one):

| Endpoint | Permission | Behaviour |
|----------|-----------|-----------|
| `POST /agent/runs/{run_id}/replay` | `READ` | Re-run the objective by substitution and return a per-step diff plus any divergence. Read-only — never mutates the recording or produces a new persisted run. |
| `POST /agent/runs/{run_id}/fork` | `RUN_AGENT` | Counterfactual fork: replay up to `fork_index`, then from the fork on, LLM completions run against a **different** model (`model`, optionally with a `prompt_override` appended to the system prompt) and tool dispatches run **live**. If the pre-fork prefix no longer reproduces the tape, the fork aborts and the divergence is reported. |

The `scf-replay` CLI (`python -m scf_api.cli_replay`,
`apps/api/scf_api/cli_replay.py`) loads a persisted cassette by id, replays a
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
in `packages/multi-agent-coordinator/scf_coordinator/red_team.py`
(`run_red_team`).

### Data model

The `red_team_record` table (migration `0038`,
`packages/db/scf_db/models/red_team.py`) is append-only via the shared
immutability trigger. Columns: nullable `workflow_run_id`, `verdict`
(`blocked`, `survived` or `not_run`), `kind` (`failing_test`, `spec_violation`
or `not_run`; `parked` on records made before #174), `evidence` (the structured
attack result, and the `phase` the scan ran at: `spec` or `pr`), and the
heterogeneous `adversary_model` / `coder_model` pair (both nullable — a
`not_run` record has no adversary model). The table has no phase column, so the
phase lives in `evidence`; a record without one is a `spec` scan.

### API / CLI surface

The gate runs on **both** engine spines, minting the same verdict contract
through the shared `scf_workflow.red_team_gate` helper:

- **Temporal (V2)** — driven inside the workflow
  (`packages/workflow-engine/scf_workflow/temporal/workflows.py`): after the
  spec is drafted and clarified but **before** the human spec-approval gate, the
  workflow runs a red-team scan. A `blocked` verdict routes the change back for
  changes (→ clarification → spec review) so a human must address the finding;
  any other verdict proceeds unchanged. The `forge.run_red_team_scan` activity
  (`packages/workflow-engine/scf_workflow/temporal/activities.py`) returns
  exactly `self._red_team_fn(inp)` and persists nothing itself; the worker's
  bound `red_team_fn` (`apps/worker/scf_worker/temporal_services.py`) records
  the `RedTeamRecord` and, on a survive, the `redteam.survived` audit event
  through `ensure_red_team_verdict`, once per run and phase. At the `pr` phase,
  after the checks and the independent review pass, `forge.run_checks` runs the
  real adversary and records its verdict once per head commit (#283), as
  described under [The governed pipeline on Temporal](#the-governed-pipeline-on-temporal).
- **V1 (Postgres FSM)** — the FSM is a plain transition graph with no gate
  hooks, and its one production driver is the workflow router: when a
  transition lands a V1 run in `spec_review` (the human spec gate — the same
  position the Temporal spine scans), the handler **unconditionally** mints the
  run's verdict once per phase via `ensure_red_team_verdict` (idempotent across
  gate re-entries: a block → changes → resubmit loop does not rescan) and appends it
  to `red_team_record` with the same `redteam.survived` audit chaining — no
  extra step or injected recorder required.

The verdict is surfaced at `GET /workflow/runs/{run_id}/red-team`
(`apps/api/scf_api/routers/workflow.py`), returning the latest verdict plus the
full scan history, each record with its `phase` (a `blocked` scan followed by a re-submitted `survived` one is
common). It reads the append-only table scoped to the caller's workspace on the
row itself; an unscanned or foreign run reads as `latest=None` with an empty
history — never a 404, so the endpoint never leaks cross-tenant existence.

A scan can also be triggered explicitly: `POST /workflow/runs/{run_id}/red-team`
(write permission, same pattern as the router's other write endpoints) runs the
configured adversary — or records `not_run` when none is wired —
and **appends** a fresh verdict to the run's history, returning `202` with the
new `record_id` (+ `verdict`/`kind`); the follow-up GET returns it. It answers
`409` when the run is not at a gateable state (`spec_review` for the spec
phase; `pr_opened`/`awaiting_review` for the pr phase) and `404` for unknown or
foreign runs (no existence leak).

### Current limitations

- **`not_run` when no adversary is wired (#174, FR-017).** The default
  `red_team_fn` on both spines (`scf_workflow.red_team_gate.not_run_verdict`,
  which the Temporal activity's `_default_red_team` and the API's
  `get_red_team_fn` default to) records `verdict=not_run`, `kind=not_run`, with
  the reason in its evidence: `no_candidate_diff` at the `spec` phase (there is
  no code to attack yet) and `red_team_not_configured` at a later phase. A stand-in
  is never recorded as `survived`, chains no `redteam.survived` audit event, and
  the approval page's badge shows it as "Red-team scan not run". Records made
  before #174 as `survived`/`parked` show the same way.
- **It is persisted on both spines.** The V1 router transition handler calls
  `ensure_red_team_verdict` when a run reaches `spec_review`. The Temporal
  worker binds the same function to `forge.run_red_team_scan`
  (`apps/worker/scf_worker/temporal_services.py`), so a Temporal run also has
  one `not_run` row at the `spec` phase, and one row per head commit at the
  `pr` phase. A retried scan records no second row. A `WorkflowActivities`
  built without a `red_team_fn` and without `allow_fakes` refuses the scan
  instead of passing it.
- **The adversary runs on the Temporal spine only (#283).** The V1 router and
  the explicit trigger keep the `not_run` default; on Temporal a run with no
  adversary configured stops for a person instead (FR-017).
- **Two overlapping scans of one head commit can disagree (#283).** The
  once-per-head check reads before it records, with no lock or unique index. When
  a checks activity times out, its thread runs on beside the retry, so both can
  run an adversary and record different verdicts (say `blocked` and
  `survived`) for the same head commit. Each row is an honest scan; the
  attestation reads the newest.

---

## Self-Eval Gate

### What it is

The **Self-Eval Gate** refuses a model / prompt / router **config change** if,
re-evaluated on a workspace's **private per-repo regression suite**, its
resolution rate drops below a frozen baseline. The private suite is a *living
accumulator* minted from the org's own merged PRs: each merge can mint one hidden
regression case, and those minted test ids live only on disk in the suite dir —
they never enter a model prompt.

The gate itself (`packages/evaluation/scf_eval/sweval/gate.py`,
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
  (`packages/db/scf_db/models/benchmark.py`, `SelfEvalBaseline`): exactly one
  baseline per `(workspace, suite)` (unique constraint), carrying
  `baseline_rate`, `resolved`, `total`, a **redacted** `config` snapshot (no
  secrets ever land in this table), and `recorded_by`. A run upserts the row.

`SelfEvalService`
(`apps/api/scf_api/services/self_eval_service.py`) is the storage layer:
`workspace_baseline` is the gate's baseline lookup (most-recently-updated wins;
`None` on cold start), and `record_baseline` upserts — with `overwrite=False`, an
existing baseline is left untouched so a regressing run can never silently
*lower* the bar it defends.

### API / CLI surface

The gate is consulted at the Adaptive Orchestration config-change API
(`apps/api/scf_api/routers/ao_settings.py`) on `PUT /ao/role-config/{role}`
and `PUT /ao/settings`. Enforcement is behind the `self_eval_enforce` app
setting (a no-op unless set). On a regression the block is **audited**
(`ao.config.self_eval_blocked`, its own committed row) and a `409` is raised
*before* the mutation is applied; a `force=true` override is audited
(`ao.config.self_eval_forced`) and allowed through.

Two thin endpoints back the web panel (same router):

- `GET /ao/self-eval/status` (READ) — raw facts, no derived verdicts: the
  `self_eval_enforce` flag, the workspace's private suite (published
  preferred; case content is never exposed), and the most recently updated
  baseline (`baseline_rate`, `resolved`/`total`, `recorded_at`). `null`s on
  cold start.
- `POST /ao/self-eval/runs` (ADMIN, `202`) — enqueues the worker-owned
  `forge.self_eval.run` Celery task with the workspace's current effective AO
  config snapshot (redacted) via the api->worker `send_task` seam
  (`enqueue_self_eval_run` in
  `apps/api/scf_api/services/self_eval_service.py`, mirroring
  `pm_service.py`). `409 no_private_suite` when no published private suite
  exists (nothing the worker could score); the request is audited
  (`ao.self_eval.run_requested`). The run itself still executes only in the
  worker — this endpoint queues it, nothing more.

Two worker tasks own the offline, agent-driven halves (they run in the worker,
never a request path, because a real run is minutes-long):

- `forge.self_eval.mint` (`apps/worker/scf_worker/tasks/self_eval_mint.py`) —
  mints a hidden regression case from each merged PR and appends it to the
  private suite, re-freezing the manifest with a fresh `content_hash`.
- `forge.self_eval.run` (`apps/worker/scf_worker/tasks/self_eval_run.py`) —
  drives the `ProductionEvalRunner` over the private suite and records the
  baseline the gate blocks against. It is an honest **no-op** until an operator
  provisions the prerequisites (a published private suite, `FORGE_BENCHMARK_DIR`,
  a local clone under `FORGE_SELF_EVAL_REPO_ROOT/<repo_id>`, and a BYOK model) —
  every missing piece resolves to a `scored: false` reason rather than
  fabricating a score.

### Current limitations

- **Phase A: the API-layer gate no-ops without an injected runner.** The default
  eval runner at the API layer (`_unavailable_runner` in
  `apps/api/scf_api/services/self_eval_gate.py`) returns `None`, because
  running a workspace's private suite is a minutes-long worker job and
  `apps/api` cannot import `scf_worker` without an import cycle. So at
  config-change time the API gate has **no fresh scorecard for the proposed
  config and no-ops** — the gate *mechanism* (baseline lookup, regression block,
  force override, audit) is fully wired and exercised in tests by injecting a
  runner, but a stock API deployment does not evaluate the proposed config
  inline.
- **Enforcement is off by default** (`self_eval_enforce`).
- **Baseline establishment/refresh is worker-executed.** The run is the
  `forge.self_eval.run` Celery task; `POST /ao/self-eval/runs` is only a thin
  admin trigger that enqueues it (there is still no inline run in any request
  path — `benchmarks.py`'s "no inline `POST /runs`" note stands for the
  community-suite router).
- **Web UI**: the Self-Eval Gate settings panel
  (`apps/web/src/components/self-eval/self-eval-panel.tsx`, rendered on
  Settings -> "Models & effort" beneath the AO config it guards) shows the
  private suite, the frozen baseline (rate + recorded-at), the last scoring
  run, the derived gate posture, and a "Run self-eval" action over
  `POST /ao/self-eval/runs`. The Phase-A limitation above is stated inline in
  the panel — a no-baseline workspace is told plainly that the gate cannot
  block until a baseline exists. Phase B (inline evaluation of the proposed
  config at the API layer) remains out of scope.
