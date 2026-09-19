# Software Factory Programme — roadmap A–F

**Date:** 2026-09-19
**Status:** Approved (decomposition and sequencing)
**Scope:** Close Forge's gaps against the software-factory reference model, and make the
standard path the fast path.

---

## 1. Why this programme exists

Forge is measured here against an external reference model: *"The perfect software factory"*.
That model defines a software factory as

> the standardised, instrumented path from a business idea to a measured production outcome,
> owned as a product, with the controls a given risk class requires built into the path rather
> than applied to it.

It splits delivery into two loops:

- **Inner loop** — specification → change → review → test → merge → learn.
- **Outer loop** — idea validated cheaply → released under proportionate control → effect
  measured → measurement decides what happens next.

Its central diagnostic: *can one team carry one idea around the outer loop without a handover?*

Forge today is a strong inner-loop factory that instruments itself as though the inner loop were
the whole system. This programme builds the outer loop and the risk spine that makes it coherent.

## 2. Assessment as of 2026-09-19

Measured against the model's ten capabilities, at commit `6b9e3d3`.

| # | Capability | State | Evidence in repo |
|---|---|---|---|
| 1 | Intent validation | Strong | `SpecManifest`, `AcceptanceCriterion`, spec-engine validation, spec-studio drafting |
| 2 | Specification and contracts | Strong | `forge_contracts` DTOs, R→A→T→test traceability, `spec-engine/dashboard.py` |
| 3 | Knowledge | Strong | pgvector + BM25 + RRF(k=60) + reranker; provenance and invalidation present |
| 4 | Orchestration | Strong | workflow FSM, Temporal, `multi-agent-coordinator`, approvals, leases |
| 5 | Execution | Strong | worktree / container / gVisor / Firecracker sandbox tiers, audit records |
| 6 | Quality evidence | Strong | 436 test files, self-eval gate, red-team gate, attested changesets |
| 7 | Progressive delivery | **Partial** | `deploy-core` FSM, gate, freeze windows, rollback — but no risk classification, no flags, no canary |
| 8 | Outcome measurement | **Partial** | DORA, incidents, observability — but lead time is a proxy, no experimentation, no semantic layer |
| 9 | Governance | Strong | policy-as-code, audit log, RBAC, SSO/SCIM, envelope secrets, attestation |
| 10 | Economics | **Partial** | cost meter, budgets, FX — but no cost per delivered change, no spend-vs-value |

### Verified absences

Each returned zero matches across `packages/`, `apps/`, `docs/`:

- `risk.class|RiskClass|risk_class` — **0 files.** The model calls risk classification *"the single
  highest-leverage structural decision in a regulated setting."* Forge averages risk instead: the
  deploy gate keys off **environment**, never off change risk.
- `cost_per|cost-per` — **0 files.** Cost is metered per agent run in tokens, never bound to a
  delivered change, so spend-against-value cannot be computed.
- `paved road|golden path` — **0 files.**
- `feature.flag` — 3 incidental matches, no flag system.
- `canary` — 9 matches, all in the replay/fork subsystem, unrelated to deployment.

### The instrumentation gap, in the code's own words

`packages/observability/forge_obs/analytics/dora.py` is candid in its docstring:

> *"'lead time for changes' is properly commit-to-production; this foundation's `deployment` row
> carries no commit-authored/merged timestamp (only `commit_sha`), so `lead_time_seconds` uses
> `requested_at -> finished_at` (the promotion's own request-to-live duration) as the measurable
> proxy — documented here rather than silently approximated."*

That is inner-loop instrumentation wearing an outer-loop label. The honesty is commendable; the
gap is real. Sub-project **C** closes it.

### Not a gap: Kubernetes and Docker

The original request asked to "introduce Kubernetes and Docker support where and if needed". It is
not needed. Already present:

- Helm chart with 11 unit tests, kind-based e2e, gVisor and Kata RuntimeClasses, NetworkPolicies,
  PodDisruptionBudgets, ServiceMonitor, pinned postgresql/redis/minio subcharts
- Four Dockerfiles (api, web, worker, mcp-gateway) plus a hardened Compose stack
- OpenTofu IaC for dev/staging/prod under `infra/`

Adding container or orchestration support would add nothing. Where this programme touches
deployment it extends `deploy-core` and the existing chart, never replaces them.

## 3. Sub-projects

Six, sequenced. Each gets its own spec → plan → implementation cycle.

| ID | Name | Depends on | Closes |
|----|------|-----------|--------|
| **F** | Local-CLI model provider | — | Prerequisite for dogfooding; no capability gap |
| **A** | Risk classification spine | F (for dogfood only) | Capability 7 (classification half) |
| **B** | Progressive delivery | A | Capability 7 (mechanics half) |
| **C** | Outer-loop measurement | A | Capability 8 |
| **D** | Change economics | C | Capability 10 |
| **E** | Paved-road adoption | A–D | The "anyone can use it" half |

`A → B` and `C → D` are ordered. The two pairs are independent of each other. **E** is most
honest last, because it surfaces the numbers the others produce.

### The spine

**A establishes changeset identity** — a stable, auditable answer to *"which change is this?"*
bound to derived risk, gate decisions, commit SHA and PR numbers. Everything downstream hangs
from it:

- **B** consumes `GateRequirements.deploy_mode` to select canary/ring behaviour
- **C** joins telemetry to that identity to answer *"did the last ten changes work?"*
- **D** aggregates cost against the same identity for cost-per-delivered-change
- **E** surfaces the agreement rate and the model's ten diagnostic questions

This is why A is first among the capability projects: without one identity, B–D each invent their
own and the outer loop never closes.

## 4. Principles this programme commits to

Taken from the reference model, and binding on every sub-project below.

1. **Automation follows trust, never precedes it.** Every gate-affecting change ships in shadow
   mode first, recording what it *would* have decided, and flips to enforcing only on measured
   agreement. A gate that depends on human discipline at machine speed is not a control.
2. **Risk is classified, not averaged.** A docs typo and a change to a money path must not travel
   the same path at the same speed.
3. **Enforcement is monotone-tightening.** Risk may add scrutiny; it may never remove any that
   exists today. A misconfiguration must fail loud and closed, never silently permissive.
4. **Measure the system, never the individual.** No sub-project introduces a per-person metric.
5. **The paved road must be the fastest road.** Adoption is won by being faster, not by mandate.
   The only mandatory items are controls; conveniences stay voluntary.
6. **Volume is not value.** No sub-project reports generated lines, PR counts or token spend as an
   outcome measure.

## 5. Sub-project summaries

Full specs are separate documents. F and A are implementation-ready. B–E are drafts at scope and
interface depth — see §6.

### F — Local-CLI model provider
`2026-09-19-local-cli-provider-design.md`

Let Forge drive locally installed agent CLIs (`claude`, `codex`) when no BYOK API key is
configured, so a self-hoster can use an existing CLI subscription. A host-side bridge over
loopback solves the architecture mismatch between a Darwin/arm64 host and a Linux/aarch64 worker
container.

### A — Risk classification spine
`2026-09-19-risk-classification-spine-design.md`

A new `risk-core` package deriving a risk class deterministically from the diff, a `risk_rules:`
block in `.forge/policy.yaml`, two append-only tables, and a `RiskReader` seam threaded into the
five existing gates — shadow first, enforcing per-repo on measured agreement.

### B — Progressive delivery *(draft)*
`2026-09-19-progressive-delivery-design.md`

Feature flags, canary and ring deployment, and rollback measured as a rehearsed number rather
than documented as a procedure. Consumes A's `deploy_mode`.

### C — Outer-loop measurement *(draft)*
`2026-09-19-outer-loop-measurement-design.md`

True commit-to-production lead time with the waiting fraction separated from active work,
telemetry bound to what shipped, and a per-change outcome verdict answering *"did the last ten
changes work?"*.

### D — Change economics *(draft)*
`2026-09-19-change-economics-design.md`

Cost per delivered change including review, rework and support; spend against delivered value.
Depends on C's change identity and outcome verdicts.

### E — Paved-road adoption *(draft)*
`2026-09-19-paved-road-adoption-design.md`

Golden-path onboarding, adoption instrumentation (what proportion of eligible work uses the
standard path, and why the rest does not), and the model's ten diagnostic questions as a real
self-assessment surface backed by live queries.

## 6. On specifying B–E now

These four are written to scope, interface and acceptance-criteria depth, not to
implementation depth. That is deliberate.

The reference model's failure mode #4 is *"automating an unstable process — encoding a process
still in flux, then maintaining the encoding instead of the process."* Specifying B–E's internals
before A exists would commit exactly that error: A's changeset identity and gate seam are the
substrate all four build on, and their real shape will be known only once A is running in shadow
mode against real changes.

Each draft therefore states what it must achieve and what it depends on, and defers how. Each will
be revised from A's learnings before its own plan is written.

## 7. Programme-level acceptance

The programme is done when Forge can answer the model's ten diagnostic questions from live data.
Per sub-project:

| Question | Answered by |
|---|---|
| Time from idea to production, and the waiting fraction | C |
| One team, one change, no handover | E (demonstrated on a real change) |
| What the test suite actually catches | Already: seeded-fault measurement in `evaluation` |
| What happens when an automated actor gets stuck | Already: leases + escalation artifacts |
| How fast a release can be reversed | B (rehearsed number) |
| Did the last ten changes work | C |
| What the last change cost to deliver | D |
| Which changes may ship without a release event, and who decided | A |
| Proof that a deletion request was honoured | Already: audit + receipts |
| Proportion of eligible teams on the standard path, and why not | E |

Four of ten are already answerable. This programme closes the remaining six.

## 8. Exit criteria

The reference model insists a factory programme states what "done with the disruptive phase"
means and then declares it. For this programme:

**Done** is when all six sub-projects have shipped, every gate-affecting change has flipped from
shadow to enforcing on at least one real repository with a recorded agreement rate, and the ten
questions above are answerable from live queries rather than from a person.

After that point this becomes continuous improvement, not a programme, and the roadmap document
is retired rather than extended.
