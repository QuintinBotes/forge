# Feature Specification: Proof

**Feature Branch**: `005-proof`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M5 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`, issues R23 to R27): "Prove it in public: forge builds forge from its own issues, with published metrics."

**Builds on**: lettered spec J (`docs/superpowers/specs/2026-11-07-context-memory-and-lines-design.md` section 2.4), lettered spec M (`docs/superpowers/specs/2026-11-07-bring-your-own-tracker-design.md`), the unified programme (`docs/superpowers/specs/2026-11-07-unified-factory-programme.md` section 5), ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed), ADR-0010 (licence and clean-room policy), ADR-0018 (retrieval degrades honestly).

## Why this exists

Forge measures one thing today: retrieval quality (`docs/EVAL_RESULTS.md`). Nothing measures whether an issue handed to forge becomes a correct, landed change, what that costs, or how often a person has to step in. The only agent benchmark in the repository, `forge-swe` 1.0.0, holds five cases in 37 lines and has no tests behind its agent cases. The context benchmark that spec J names as its bar has not been written. Six of the ten Beta release gates have never run because no credentials were supplied, and no release is tagged.

A factory that asks to be trusted has to show its numbers and show itself doing the work. This feature defines the numbers, builds the benchmarks, runs the gates, and then has forge deliver its own roadmap.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read the factory's five numbers (Priority: P1)

An engineering lead who is deciding whether to let forge work on a repository asks for a time window and gets five numbers for it: autonomous merge rate, cost per merged change, revert rate, human intervention rate and cycle time. Each number comes with its written formula, its sample size, and the evidence rows it was computed from.

**Why this priority**: Every later claim (the benchmark, the dogfood report, the open specification in M8) quotes these metrics. Without one definition, each report would invent its own.

**Independent Test**: Load a fixed set of recorded runs, changes, cost events and interventions, request the metrics for a window, and compare each value with the hand-computed result of the written formula.

**Acceptance Scenarios**:

1. **Given** a window in which 10 attempts settled, 6 landed, 4 of those 6 had no human intervention, **When** the lead requests the metrics, **Then** autonomous merge rate is 0.40 and the response states the numerator 4 and the denominator 10.
2. **Given** a window in which no change landed, **When** the lead requests the metrics, **Then** cost per merged change and cycle time are reported as not available with the reason, and never as zero.
3. **Given** a landed change that a later change reverted inside the observation period, **When** the metrics are requested, **Then** the revert is counted against the window in which the original change landed.
4. **Given** a change that landed fewer than 30 days ago, **When** the metrics are requested, **Then** revert rate for that window is marked provisional.
5. **Given** two workspaces, **When** one requests its metrics, **Then** no run, change or cost of the other contributes.

---

### User Story 2 - Score the whole pipeline on tasks it has never seen (Priority: P1)

A maintainer changes a model, a prompt or a gate and wants to know whether the factory got better or worse. They run `forge-factory-bench`: each case is an issue, a repository at a base commit, and tests that the implementing agent never sees. The harness feeds the issue in, takes the change that comes out, runs the held-out tests in a sandbox, and reports how many cases were resolved, how many resolved cases also carried a passing verification record, the cost and the time.

**Why this priority**: The five metrics describe production history. A benchmark is the only way to compare two configurations on the same work before shipping one of them.

**Independent Test**: Run the harness with the reference solver that applies each case's known-good patch (all cases resolve) and with a solver that returns an empty patch (no case resolves). Both results follow from the held-out tests alone.

**Acceptance Scenarios**:

1. **Given** a frozen suite version, **When** any case file or the scoring rubric is edited, **Then** every benchmark command rejects the suite until a new version is created.
2. **Given** a case with held-out tests, **When** the harness hands the case to a solver, **Then** the solver receives no held-out test id, path or content.
3. **Given** a solver whose patch makes the fail-to-pass tests pass but breaks a pass-to-pass test, **When** the case is scored, **Then** it is not resolved and the regressed test is named.
4. **Given** a completed run, **When** the report is written, **Then** it states the suite version and content hash, the forge commit, the model identifiers, the sandbox tier, per-case outcomes and totals, and it can be reproduced from the published harness.

---

### User Story 3 - Hold context assembly to a bar (Priority: P2)

A maintainer who changes retrieval, the world model or context assembly runs a 60-question benchmark. Each question is one an agent would ask before working ("which module owns X", "what did we decide about Y"). The benchmark assembles context for each question and scores whether the context contains what is needed to answer, how large it is, and, in the model-graded lane, whether the answer is right and free of unsupported claims.

**Why this priority**: Spec J names this benchmark as its acceptance bar. It ranks below the first two stories because it measures one component, not the factory.

**Independent Test**: Run the benchmark's hermetic lane over its committed corpus and compare the score with the committed floor.

**Acceptance Scenarios**:

1. **Given** the committed corpus and question set, **When** the hermetic lane runs, **Then** it reports a sufficiency score, the median context size in tokens, and a pass or fail against the committed floors.
2. **Given** a change that lowers the sufficiency score below the floor, **When** the test suite runs, **Then** it fails.
3. **Given** a missing retrieval leg, **When** the benchmark runs, **Then** it reports an error for the run and never a score computed from the remaining leg (ADR-0018).
4. **Given** model credentials, **When** the model-graded lane runs, **Then** it reports answer accuracy as a measurement and the rate of unsupported claims against the 7 percent ceiling. No pass bar is applied to answer accuracy until one is set from the recorded baseline (#249).

---

### User Story 4 - See the release gates run for real (Priority: P2)

The owner supplies credentials for a model provider, a GitHub App, an MCP server, Slack and Postgres, and runs the readiness report. The six gates that were skipped now run. The owner fixes or records what they find and tags a release when the Beta bar is met.

**Why this priority**: Until these gates run, "works in tests" is the strongest statement forge can make. It ranks P2 because it needs the owner and cannot be done by the loop.

**Independent Test**: Regenerate `RELEASE_READINESS.md` with all six gate environments set and confirm that no gate reports `SKIPPED_NO_CREDS`.

**Acceptance Scenarios**:

1. **Given** credentials for all six gates, **When** the readiness report is generated, **Then** each of `G-DB`, `G-MODEL`, `G-RAG-REAL`, `G-GH`, `G-MCP` and `G-SLACK` reports `GREEN` or `RED` with evidence, and none is skipped.
2. **Given** a `RED` gate, **When** the owner reviews the report, **Then** a tracked issue exists for it and the Beta bar is reported as not met.
3. **Given** the Beta bar is met, **When** the owner tags the release, **Then** the tag points at the commit named in the readiness report.

---

### User Story 5 - Forge delivers its own roadmap (Priority: P3)

The owner labels an open roadmap issue `forge:run`. Forge starts a run from the issue, the run goes through the governed pipeline, and a pull request with an attestation appears. The owner reviews it. The issue queue that Orbit served is now served by forge, and the five metrics for this work are published.

**Why this priority**: This is the public proof, and it depends on the governed pipeline (M1) and automation lines (M2) being in place.

**Independent Test**: Label one small roadmap issue on the forge repository and follow it to a pull request that carries a verifiable attestation, with a write-back comment on the issue.

**Acceptance Scenarios**:

1. **Given** an open roadmap issue with all dependencies closed, **When** it receives the `forge:run` label from an allowed sender, **Then** exactly one run starts and the issue receives one run-state comment.
2. **Given** a roadmap issue labelled `owner-decision` or `needs-credentials`, **When** the queue is served, **Then** the issue is skipped.
3. **Given** a run that fails or is escalated, **When** the queue is served, **Then** the queue stops, the issue is marked blocked, and no later issue starts.
4. **Given** at least five roadmap issues delivered this way, **When** the dogfood report is generated, **Then** it lists each issue, its run, its pull request, its attestation verification result and the five metrics for the set.

---

### Edge Cases

- A run produces no pull request. It counts as a settled attempt with outcome "no change", and its cost counts toward cost per merged change.
- A pull request is closed and later reopened and landed. The final outcome wins and the attempt is counted once.
- A change is reverted and the revert is itself reverted. The original change stays counted as reverted.
- A person approves a required gate. This is governance, not an intervention, and does not lower autonomous merge rate. A rejection or a request for changes is an intervention.
- Sub-agent runs of a supervised run. They contribute cost to their parent attempt and are never counted as attempts.
- A cost event has no price. The metric is reported with the count of unpriced events, not silently lowered.
- A benchmark case's setup command fails. The case is reported as an infrastructure error, separately from "not resolved".
- A benchmark run is interrupted. Finished cases keep their results and a rerun resumes the rest.
- The context benchmark corpus drifts from the question set. The benchmark runs over a corpus pinned by content hash, not over the live repository.
- A dogfood run is asked to change a path the loop protects (CI definitions, its own policy). The run is refused and the issue is marked blocked.

## Requirements *(mandatory)*

### Functional Requirements

**Metrics (R23)**

- **FR-001**: Forge MUST publish one document that defines the five factory metrics with formulas, populations, exclusions and worked examples. The definitions below are normative.
- **FR-002**: An **attempt** is a top-level agent run. It is **settled** when the run is terminal and its change, if it produced one, has a final outcome (landed or closed without landing). A metrics window selects attempts by settle time. `S` is the set of settled attempts in the window and `L` is the subset whose change landed.
- **FR-003**: **Autonomous merge rate** MUST be `|{a in L : interventions(a) = 0}| / |S|`.
- **FR-004**: **Cost per merged change** MUST be `sum(cost(a) for a in S) / |L|`, where `cost(a)` includes the attempt's sub-agent runs. The cost of attempts that did not land is included in the numerator.
- **FR-005**: **Revert rate** MUST be `|{a in L : reverted(a)}| / |L|`, where `reverted(a)` means a later landed change reverted it within 30 days of landing. A window whose newest landed change is younger than 30 days MUST be marked provisional.
- **FR-006**: **Human intervention rate** MUST be `|{a in S : interventions(a) >= 1}| / |S|`. An intervention is one of: a rejection or request for changes on a gate tied to the attempt; a review on the change that requests changes; a commit on the change's branch by an identity other than forge; a run that ended escalated to a person. Approving a gate is not an intervention.
- **FR-007**: **Cycle time** MUST be reported as the median and the 90th percentile, over `L`, of the time from the attempt's creation to the moment its change landed.
- **FR-008**: Every metric MUST be returned with its numerator, denominator, sample size and window, and MUST be reported as not available (with a reason) when its denominator is zero.
- **FR-009**: Forge MUST record, for each attempt, the final outcome of its change with timestamps, each intervention with its kind, and any revert, from events it receives or decisions it records. A record MUST be idempotent under redelivery.
- **FR-010**: Metrics MUST be scoped to one workspace and MUST be filterable by repository and by how the attempt started (API call or tracker dispatch).
- **FR-011**: The metrics MUST be available through the API and through `forge-cli` with JSON output that carries the provenance fields of ADR-0015.

**Factory benchmark (R24)**

- **FR-012**: Forge MUST ship a benchmark suite named `forge-factory-bench` in which each case consists of an issue text, a repository state, and held-out tests split into fail-to-pass and pass-to-pass.
- **FR-013**: Held-out tests MUST never be visible to the solver: not their ids, paths or content.
- **FR-014**: A case is resolved only when every fail-to-pass test passes and no pass-to-pass test fails, with tests run in a sandbox.
- **FR-015**: The harness MUST accept any solver through one interface, MUST ship a reference solver for self-test, and MUST be able to drive a running forge instance as the solver.
- **FR-016**: The report MUST state, per run: suite version and content hash, forge commit, model identifiers, sandbox tier, per-case outcome (resolved, not resolved, infrastructure error), resolve rate, the share of resolved cases whose change passed forge's own verification, cost and wall time.
- **FR-017**: A released suite version MUST be frozen by content hash. `forge-swe` 1.0.0 stays frozen and unedited, and documentation MUST stop presenting it as an agent benchmark.
- **FR-018**: No case may require a change to land without a person's approval (ADR-0005). Cases end no later than a pull request awaiting review.

**Context benchmark (R25)**

- **FR-019**: Forge MUST ship a context benchmark of 60 questions with, for each question, the evidence a correct answer needs. Questions, answers and corpus MUST be written new for forge (ADR-0010). No text from another project's benchmark may be copied.
- **FR-020**: The hermetic lane MUST score context sufficiency (the share of questions whose assembled context contains all required evidence) and the median context size in tokens, with no network and no model, over a corpus pinned by content hash.
- **FR-021**: The hermetic lane MUST fail the test suite when a score falls below its committed floor.
- **FR-022**: The model-graded lane MUST report answer accuracy and the rate of unsupported claims, MUST be opt-in, and MUST skip visibly without credentials.
- **FR-023**: Decided 2026-11-15: the pass bar for answer accuracy is measured first. The 0.86 figure of spec J section 2.4 is not carried over. #199 writes forge's own questions and records a baseline, and a follow-up (#249) sets the bar from that measurement. Until then the model-graded lane reports answer accuracy with no pass bar. Spec J's other two figures, unsupported claims at or below 7 percent and median context at or below 17,800 tokens, are not covered by the decision and stay as stated. Open: the rule that turns the baseline into a bar.

**Live gates and release (R26)**

- **FR-024**: The readiness report MUST be regenerated with credentials for all six skipped gates, and each gate MUST report a result with evidence.
- **FR-025**: No credential may appear in the report, in logs, or in the repository.
- **FR-026**: A release tag MUST be created only when the readiness report states that the Beta bar is met, and MUST point at the commit the report names.

**Dogfood (R27)**

- **FR-027**: The roadmap queue MUST be servable by forge's own issue dispatch: one issue at a time, in dependency order, skipping issues labelled `owner-decision`, `needs-credentials` or `blocked`.
- **FR-028**: The queue MUST stop on the first run that does not end in a pull request with a passing verification record, and MUST mark that issue blocked.
- **FR-029**: Forge MUST publish a dogfood report that lists each delivered roadmap issue with its run, pull request and attestation verification result, and the five metrics for the set.
- **FR-030**: A dogfood run MUST work under the same policy as any other run. It MUST NOT be able to change its own policy, CI definitions or credentials.

### Key Entities

- **Attempt**: One top-level agent run and everything it caused. Has a start source, a repository, a cost, and at most one change.
- **Change outcome**: The final state of an attempt's pull request: landed or closed without landing, with timestamps, and whether it was later reverted.
- **Intervention**: One recorded act by a person that altered or rejected an attempt's work. Has a kind and a time.
- **Metrics report**: The five metrics for a workspace, window and filter, each with numerator, denominator and sample size.
- **Benchmark case**: An issue, a repository state and held-out tests.
- **Benchmark report**: The scored outcome of one harness run over one frozen suite version.
- **Context question**: A question, the evidence a correct answer needs, and the claims a correct answer must not make.
- **Readiness report**: The gate-by-gate result that decides whether a release bar is met.
- **Dogfood report**: The list of roadmap issues forge delivered, with evidence and metrics.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a fixed fixture of recorded history, each of the five metrics equals the hand-computed value of its written formula.
- **SC-002**: A reader can compute any of the five metrics from the definitions document and the evidence rows alone, without reading code.
- **SC-003**: `forge-factory-bench` 1.0.0 contains at least 20 cases, and the reference solver resolves all of them while an empty solver resolves none.
- **SC-004**: A third party can reproduce a published benchmark report from the suite version, the harness and the stated configuration.
- **SC-005**: The context benchmark has exactly 60 questions and its hermetic lane completes in under two minutes on a developer machine.
- **SC-006**: The readiness report shows zero gates skipped for missing credentials.
- **SC-007**: At least five roadmap issues are delivered through forge's own dispatch, each with a verifiable attestation, and the dogfood report is published in the repository.
- **SC-008**: After the queue moves to forge, no roadmap issue is implemented through the earlier loop.

## Assumptions

- M1 (spec 001) delivers the governed pipeline, held-out scenarios (R09) and attestations (R08) that the benchmark and the dogfood report read. M2 (spec 002) delivers the station workflow (R11).
- GitHub is the only tracker and code host in scope. The outcome, review and revert signals come from GitHub webhook deliveries that forge already receives.
- Cost is the priced model usage forge records per run. Infrastructure cost and human time are out of scope.
- The 30-day revert observation period is a default and may be configured.
- Published figures from other vendors or benchmarks are not used as targets. The 0.86 bar that spec J states is not carried over (decided 2026-11-15). The only external numbers left in this spec are spec J's 7 percent and 17,800 token ceilings.
- True lead time and per-change production outcomes belong to M10 (R46). Cycle time here ends when the change lands.
- Benchmark cases use small repositories written for the suite. Cases drawn from public repositories are a later version.
