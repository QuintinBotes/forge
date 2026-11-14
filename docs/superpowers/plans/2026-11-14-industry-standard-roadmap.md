# Forge industry-standard roadmap

Status: Draft for owner review. Date: 2026-11-14. Audited at `main` @ `b178e7d`.

The research behind this plan is kept locally and is not committed. Many of its market and
benchmark figures come from secondary search summaries. Treat them as leads until an issue
verifies the one it depends on.

## 1. Goal and thesis

Goal: forge becomes the reference software factory, a system that takes an issue and ships a
verified, attested change, and it runs with tervin.

A plan cannot make a project the industry standard. It can put forge in the position that
standards have come from. Kubernetes, OCI and MCP followed one sequence: a working
implementation, a narrow contract others can implement, a neutral foundation, a conformance
suite. The research found no ratified software-factory standard, no whole-factory benchmark,
no standard factory metrics, and no vendor that verifies agent output beyond tests, an LLM
reviewer and CI. Buyers complain about trust, attribution and governance, not coding ability.

So the thesis is: **forge wins on verifiable delivery, not on a better coding agent.**

1. Treat agent harnesses as swappable. Do not build one.
2. Make every agent change pass independent verification and carry a signed attestation.
3. Publish the contract ("issue in, attested change out") as an open specification with a
   conformance suite, with forge as the reference implementation.
4. Prove it in public: forge builds forge from its own issues, with published metrics.

## 2. Where forge stands

| Area | State today | Evidence |
|---|---|---|
| Acting path | Works: issue label or API call to sandboxed agent to PR. No gate, review or attestation | `apps/worker`, `docs/trust-layer.md:131-135` |
| Governed path | Gates and attestations are real, but every Temporal agent, check and PR activity refuses in production | `apps/worker/forge_worker/temporal_main.py:80-85` |
| Verification | The agent grades its own work. Red-team default records "survived" with no adversary | `docs/trust-layer.md:289-298` |
| Metrics | Only retrieval is measured. `forge-swe` is a toy | `docs/EVAL_RESULTS.md` |
| Sandbox | Default `worktree` tier is a host subprocess. Escape untested | `docker-compose.yml:337` |
| Live proof | 6 of 10 Beta gates skipped for missing credentials. 13 pentest surfaces untested, 3 of them critical (PT-01, PT-02, PT-13) | `RELEASE_READINESS.md`, `docs/security/pentest-punch-list.md` |
| Specs | A, B, C, D, E, K not started. G, H, J, L partial. F, I, M done | `docs/superpowers/specs/` |
| Tervin | Seams 1 to 3 built on forge's side. Tervin's spec 023 contradicts forge ADR-0002, 0013, 0015 | spec L, tervin `specs/023` |

The single largest gap: the path that acts and the path that governs are not joined, so the
trust layer applies to nothing an agent ships.

## 3. Decisions the plan assumes

Each is the default. The owner can change any of them before issues are created.

| # | Decision | Default | Why |
|---|---|---|---|
| D1 | What "run with tervin" means | Forge runs appear in tervin as ACP sessions and are approved there. Tervin as forge's local runner is a prototype only | Tervin accepts any ACP agent today with no code change. The runner shape needs new work in both repositories |
| D2 | Which tervin | Published v2.2.0 | The v3 line is unreleased |
| D3 | Spec format | Spec Kit for all new work under `specs/NNN-slug/`. Existing lettered specs stay where they are and are cited as design input | Migrating 14 approved design docs buys nothing. One benchmark found spec completeness matters more than the tool |
| D4 | Which issues get a full spec | Features and cross-cutting work. Small fixes and docs get a plain issue | The same benchmark found no gain from specs on bug fixes, refactors and small tickets |
| D5 | Implementation loop | Orbit, one issue at a time, driven by a small script. Switch to forge's own dispatch once milestone 1 lands | Orbit already has independent review and checks. Forge's acting path has neither yet. Parallel agent PRs conflict 20 to 42 percent of the time |
| D6 | Name | Keep "forge" for now, decide in R41 | Two other factory projects already use the name |

## 4. Milestones and issues

Order of execution: M0, M1, M6 (R28 to R31), M3, M2, M4, M5, M7, M9, M8, M10. Tervin moves
early because it is a stated requirement and its first steps are small. Each issue is sized
for one Orbit run (default budget 40 files, 2000 lines). "Spec" names the Spec Kit feature
the issue belongs to.

### M0. Factory floor

| ID | Issue | Depends on |
|---|---|---|
| R01 | Done with this plan. Adopt Spec Kit: `specify init`, a constitution drawn from `FORGE_SPEC.md` and the ADRs, and a note on how it coexists with the lettered specs | None |
| R02 | Done with this plan. Add `.orbit/config.yaml`, check ids, and `scripts/roadmap-loop.sh` that feeds open `roadmap` issues to Orbit one at a time | R01 |
| R03 | Refresh stale status: spec G table, README package list, regenerate `RELEASE_READINESS.md` | None |

### M1. One governed pipeline (spec 001)

| ID | Issue | Depends on |
|---|---|---|
| R04 | Enable the Temporal agent, check and PR activities in production so an agent run goes through the workflow FSM | None |
| R05 | Re-run checks service-side and read CI back as a gate | R04 |
| R06 | Independent reviewer station on a different model family from the builder. Its verdict gates the PR | R04 |
| R07 | Replace the default red-team result with a real adversary run | R04 |
| R08 | Attach a signed attestation to every agent PR. Stable signing key on by default | R05 |
| R09 | Held-out scenarios: acceptance scenarios the implementing agent never sees, run by the verifier | R05 |

### M2. Automation lines (spec 002, lettered spec K)

| ID | Issue | Depends on |
|---|---|---|
| R10 | Line contract in `forge_contracts/lines.py` with `validate_line` | R04 |
| R11 | `StationWorkflow`: failing tests first, implement, `await_ci`, bounded retry | R10 |
| R12 | Feature-run gates go through approvals (K P0) | R10 |
| R13 | File claims and a serialised merge step so parallel runs do not collide | R11 |

### M3. Containment (spec 003)

| ID | Issue | Depends on |
|---|---|---|
| R14 | Make a container sandbox the default tier. `worktree` becomes explicit and development-only | None |
| R15 | Sandbox escape test suite (PT-02, PT-03) and published isolation levels | R14 |
| R16 | Short-lived, per-run scoped credentials. GitHub App credentials per workspace, not worker env | None |
| R17 | Supply-chain gates on agent diffs: secret scan, dependency existence and age check, SAST | R05 |
| R18 | Test and close the three critical pentest surfaces (PT-01, PT-13, PT-02) | None |
| R19 | Impact gate that holds irreversible actions for a person, and approvals that record a rationale | R12 |

### M3 additions. Container and Kubernetes execution (spec 011)

Added after the first issues were created. They sit in the M3 milestone and are listed by
issue number. #184 now depends on #237.

| Issue | Title | Depends on |
|---|---|---|
| #233 | Kubernetes pod sandbox backend for agent runs | #184 |
| #234 | Helm RBAC, network policy and a kind test for pod sandboxes | #233 |
| #236 | Script the local Postgres and Redis test services and skip Redis tests cleanly | None |
| #237 | Keep run checkouts and mirrors on the worktree volume and mount one run's subpath | None |
| #238 | Split an agent run into phases and add a one-run process entry point | None |
| #239 | Run executor setting with launch, close-out and reconciliation | #238 |
| #240 | Run each agent run in its own container on a Docker host | #237, #239, #184 |
| #241 | Run each agent run as its own Kubernetes Job | #239, #233 |
| #242 | Helm RBAC, storage, network policy and a kind test for per-run Jobs | #241, #234 |
| #243 | Extend the escape suite and the published isolation levels to the pod backend | #185, #233, #234 |

Also added: #235 (M1), which makes a Temporal run start safe under concurrency and failure.
It came out of the review of #169.

### M4. Risk and merge (spec 004, lettered spec A)

| ID | Issue | Depends on |
|---|---|---|
| R20 | `packages/risk-core`: classify a change into a risk class | None |
| R21 | Controls proportionate to risk class | R20, R05 |
| R22 | Real auto-merge with a merge queue for low-risk changes. Remove the mock | R21 |

### M5. Proof (spec 005)

| ID | Issue | Depends on |
|---|---|---|
| R23 | Five factory metrics with written definitions: autonomous merge rate, cost per merged change, revert rate, human intervention rate, cycle time | R04 |
| R24 | `forge-factory-bench`: an end-to-end benchmark with held-out tests and a published harness. Replaces the toy `forge-swe` | R09 |
| R25 | Port the 60-question context benchmark with its 0.86 bar (spec J) | None |
| R26 | Run the six skipped Beta gates with real credentials and tag a release. Needs owner credentials | R18 |
| R27 | Dogfood: forge implements its own roadmap issues through `forge:run`. Move the loop from Orbit to forge | R08, R11 |

### M6. Tervin (spec 006, lettered spec L)

| ID | Issue | Depends on |
|---|---|---|
| R28 | ADR that reconciles forge ADR-0002, 0013, 0015 with tervin spec 023, and fixes the seam 1 claim in spec L (tervin's `mcp.json` is stdio-only, forge's server is HTTP). Owner decision | None |
| R29 | `forge acp`: a stdio ACP shim in `forge-cli` that wraps run creation, the event stream and approval decisions (seam 6) | R28 |
| R30 | Widen `RunEvent.observed_by` beyond `Literal["forge"]`. Verify approval push end to end and record the deciding surface in the audit chain (seam 4) | R29 |
| R31 | Single-user compose profile, scoped personal access tokens, device-code auth | None |
| R32 | Prototype only: tervin as forge's local runner (seam 5 runner protocol) | R30 |
| R33 | Paired end-to-end test: a forge run is visible and approvable in tervin on macOS | R30, R31 |

### M7. Standards conformance (spec 007)

| ID | Issue | Depends on |
|---|---|---|
| R34 | Run the MCP conformance suite against forge's server and close the gaps to the current spec | None |
| R35 | A2A v1.0 signed Agent Card for forge | None |
| R36 | OpenTelemetry GenAI spans on agent and tool calls, pinned to one semconv version, with cost attributes | None |
| R37 | in-toto provenance for agent changes, `Assisted-by` commit trailers, CycloneDX SBOM | R08 |
| R38 | Map forge's controls to the OWASP Agentic Top 10 and Agent Baseline, with evidence links | R15, R17 |

### M8. The open standard (spec 008)

| ID | Issue | Depends on |
|---|---|---|
| R39 | Software Factory Specification v0.1: the "issue in, attested change out" contract, levels, and the evidence format | R08, R15, R23 |
| R40 | Conformance suite for the specification. Forge passes it in CI | R39 |
| R41 | Name, licence and governance decision, including a foundation sandbox proposal. Owner decision | R39 |

### M9. Product surface (spec 009)

| ID | Issue | Depends on |
|---|---|---|
| R42 | OIDC sign-in in the web app. Remove sign-in by pasted API key | None |
| R43 | Web flow to start a run from an issue, with browser end-to-end tests | R42 |
| R44 | Ten-minute self-host quickstart, verified from a clean machine | R14, R31 |

### M10. Outer loop (spec 010, lettered specs B, C, D)

| ID | Issue | Depends on |
|---|---|---|
| R45 | Feature flags, canary and automatic rollback for agent changes | R22 |
| R46 | True lead time and per-change outcomes. Replace the DORA proxy | R23 |
| R47 | Cost per delivered change and spend caps | R23 |

## 4a. Issues as created

Each roadmap issue was checked against the code before it was written. Fourteen were too large
for one run and were split; a suffixed id (R04a) is a part, and where a plain id remains (R04,
R11) it is the completing part. R01 and R02 were delivered with this plan and have no issue.

| Roadmap id | Issue |
|---|---|
| R03 | #168 |
| R04a | #169 |
| R04b | #170 |
| R04 | #171 |
| R05 | #172 |
| R06 | #173 |
| R07 | #174 |
| R08 | #175 |
| R09 | #176 |
| R10 | #177 |
| R11a | #178 |
| R11b | #179 |
| R11c | #180 |
| R11 | #181 |
| R12 | #182 |
| R13 | #183 |
| R14 | #184 |
| R15 | #185 |
| R16 | #186 |
| R17 | #187 |
| R18a | #188 |
| R18b | #189 |
| R18c | #190 |
| R19 | #191 |
| R20 | #192 |
| R21a | #193 |
| R21b | #194 |
| R22 | #195 |
| R23 | #196 |
| R24a | #197 |
| R24b | #198 |
| R25 | #199 |
| R26 | #200 |
| R27 | #201 |
| R28 | #202 |
| R29 | #203 |
| R30 | #204 |
| R31a | #205 |
| R31b | #206 |
| R31c | #207 |
| R32 | #208 |
| R33 | #209 |
| R34 | #210 |
| R35a | #211 |
| R35b | #212 |
| R36 | #213 |
| R37 | #214 |
| R38 | #215 |
| R39 | #216 |
| R40a | #217 |
| R40b | #218 |
| R41 | #219 |
| R42a | #220 |
| R42b | #221 |
| R43a | #222 |
| R43b | #223 |
| R44 | #224 |
| R45a | #225 |
| R45b | #226 |
| R46a | #227 |
| R46b | #228 |
| R47a | #229 |
| R47b | #230 |

What the code check changed:

- M1 is larger than the audit said. Nine Temporal activities refuse in production, and the
  worker cannot find a run the API created in memory, so R04 became three issues.
- The agent loop records "requires approval" as a denial and does not pause. R29 covers real
  pending approvals only.
- The approval decision route needs only `read`. R31b adds a decision permission.
- Forge's MCP server implements protocol revisions `2025-06-18` and `2025-03-26`. R34 closes
  the gap to the current revision.
- R13 updates the run branch and has no serialised integration step, which ADR-0020 rules out.
- Not scheduled in any issue: spec A's web Risk panel, spec K's installer and `verify_package`,
  spec B's rollback rehearsal, spec C's semantic layer, spec D's cost-against-outcome view.

## 5. How the loop runs

1. Every issue carries the `roadmap` label, its milestone, its dependencies, acceptance
   criteria mapped to Orbit check ids, and a link to its Spec Kit spec where it has one.
2. `scripts/roadmap-loop.sh` takes the first open `roadmap` issue, in the milestone order
   above, whose dependencies are closed, updates `main`, and pipes the issue into
   `orbit run --foreground --goal -`.
3. On success it adds `Closes #N` to the PR. A person merges it, or `AUTO_MERGE=1` turns on
   GitHub auto-merge. The loop waits for the merge, then takes the next issue.
4. On any other exit it comments on the issue, labels it `blocked`, and stops.
5. The owner starts the loop. `/stack:goal` forbids an agent from calling `orbit run`.
6. From R27 on, the same queue is served by forge's own `forge:run` dispatch.

Issues labelled `owner-decision` or `needs-credentials` (R26, R28, R41) are skipped by the
loop and wait for the owner.

Known limits of the loop:

- Orbit's intake check reads the word for integrating a branch as a request for that
  authority, even inside a file path or a metric name. R19, R21a, R21b, R22, R23, R24a and
  R47a contain it and will stop at intake for a person to answer.
- R20 edits the root `pyproject.toml`, `uv.lock` and `Makefile`, which the Orbit policy
  protects. The owner runs it by hand or widens the policy for that run.
- R33, R43b and R44 have criteria no check can prove. A person verifies them.

## 6. Risks

| Risk | What limits it |
|---|---|
| Scope: 63 issues for a solo-maintained project | Strict order. M0, M1, M6 and M3 alone make forge defensible. Everything after is additive |
| Orbit 0.3.1 can end with a verified change it did not deliver | The loop stops on any non-success exit. Expect manual rescues |
| Research figures are second-hand | No issue cites a figure as fact. R34, R36 and R38 check versions against primary specs |
| Tervin's direction contradicts forge's ADRs | R28 settles it before any tervin code is written |
| The scaffolding has a short half-life as models improve | Invest in verification and evidence, which outlast harness changes |
| Other projects already use the name | R41 |

## 7. Not covered

- Hosted or commercial offering, pricing, and community programmes.
- Lettered specs E (paved road) and H2 to H4 (world model), and the wider J index. They stay
  in the backlog until M5 shows whether they move a metric.
- GitLab, Jira and Linear triggers beyond what spec M already has.
