# Forge Implementation Slices

This directory holds the **implementation slices** for Forge — one focused, buildable
mini-plan per feature. A slice is the unit of work an engineer picks up to implement a
single feature end-to-end with no prior context.

- **Sources of truth:** [`docs/FORGE_SPEC.md`](../FORGE_SPEC.md) (product/data model/schemas/stack)
  and [`docs/forge-research-report.md`](../forge-research-report.md) (the evidence behind every decision).
  A slice never contradicts the spec; it *operationalizes* one slice of it.
- **Master index:** [`INDEX.md`](./INDEX.md) — all 39 features in one table.

---

## What an implementation slice is

A slice is a self-contained, testable build spec for exactly one feature. It is concrete
enough that an engineer with no prior context can build the feature correctly: it names the
exact package/app targets, gives real interface signatures (Pydantic models, `Protocol`s,
YAML schemas, route shapes), lists numbered testable acceptance criteria, and gives a
TDD test plan with concrete cases and fixtures. No "TBD", no vague "add error handling".

Every slice follows the **same 12-section anatomy** (verify against any file, e.g.
[`v1/F07-feature-workflow-fsm.md`](./v1/F07-feature-workflow-fsm.md)):

| § | Section | Purpose |
|---|---|---|
| 1  | Intent — what & why | The one-paragraph problem statement and the spec clause it satisfies |
| 2  | User-facing behavior / journeys | Concrete journeys / API behavior a user or caller observes |
| 3  | Vertical slice | The thin top-to-bottom path (DB → service → API → UI/CLI) that proves the feature |
| 4  | Public interfaces / contracts | Exact signatures: Pydantic models, `Protocol`s, YAML schemas, route shapes |
| 5  | Dependencies — features/slices that must exist first | Upstream slices, marked REQUIRED vs stubbable/soft |
| 6  | Acceptance criteria | Numbered, individually testable AC#1…ACn |
| 7  | Test plan (TDD) | Concrete unit + integration cases and key fixtures, written before code |
| 8  | Security & policy considerations | How the feature honors the spec's non-negotiables |
| 9  | Effort estimate & risk | S/M/L + a per-risk table/list with mitigations |
| 10 | Key files / paths | Exact files to create/edit, by repo path |
| 11 | Research references | The relevant links from the spec / research report |
| 12 | Out of scope / future | What is deliberately deferred (usually to a later phase) |

A slice is "done as documentation" when sections 4, 6, 7, and 10 are concrete enough to
implement and test without re-deriving design decisions.

---

## Folder layout

Slices are grouped by the FORGE_SPEC **phased roadmap**. The file name is `F<NN>-<slug>.md`.

```
docs/implementation-slices/
├── README.md          # this file
├── INDEX.md           # master table of all 39 features
├── v1/                # Phase 1 — Foundation (V1):  F01–F16  (16 slices)
├── v2/                # Phase 2 — Depth (V2):       F17–F26  (10 slices)
├── v3/                # Phase 3 — Scale (V3):       F27–F35  ( 9 slices)
├── cross-cutting/     # Span all phases:            F36–F39  ( 4 slices)
└── future/            # Deferred backlog:           F40 + the future spec
```

### The `future/` folder — deferred backlog

`future/` holds the **deferred-scope backlog**: everything the 39 V1/V2/V3/cross-cutting slices
explicitly pushed out of their own shippable scope in their `## 12. Out of scope / future`
section. [`future/SPEC-FUTURE-deferred-scope.md`](./future/SPEC-FUTURE-deferred-scope.md) is the
consolidated spec that harvests and de-duplicates every §12 deferral into concrete, estimable
requirements, and [`future/F40-deferred-scope.md`](./future/F40-deferred-scope.md) is the F40
roll-up slice that turns that backlog into independently-shippable, themed mini-slices. Together
**F40 + the future spec are the deferred backlog to implement *after* the V1 (and, where noted,
V2) slices** — they extend the V1 baseline rather than block it, so nothing here is on the
critical path for shipping V1.

- **`v1/`, `v2/`, `v3/`** map one-to-one to the three roadmap phases in
  [`FORGE_SPEC.md` → Phased Roadmap](../FORGE_SPEC.md). Each checkbox in a phase is one slice.
- **`cross-cutting/`** holds the platform modules that are *depended on across every phase*
  rather than living in a single roadmap checkbox: Human Approval (F36), Auth & Secrets /
  BYOK (F37), Observability & Cost (F38), and the immutable Audit Log (F39). These come from
  the spec's *Product Scope* modules (Review & Approval Layer, Auth & Secrets Service,
  Observability Layer) and *Security* table. They are built early (their substrate ships in
  V1) but evolve through V2/V3, so they are not pinned to one phase folder.

### The F00 / foundation substrate placeholder

Nearly every slice lists a dependency on the **foundation substrate** — written as
`F00-foundation-substrate` / `cross-cutting/C01-monorepo-and-api-foundations`. This is the
monorepo skeleton, the `packages/db` data model + Alembic baseline, the `packages/contracts`
shared DTOs/Protocols, the `apps/api` router skeleton, and the deploy/test infra. **There is
currently no numbered slice file for it** — it is referenced as a labeled placeholder. It is
fully specified instead by **Phase 0** of the overnight build plan (below). When that
substrate is given its own slice, it should land as `cross-cutting/C01-monorepo-and-api-foundations.md`
and every `F00*` reference in `INDEX.md` should be repointed to it.

---

## How slices map to the FORGE_SPEC roadmap

| Spec roadmap phase | Folder | Feature IDs | Count |
|---|---|---|---|
| Phase 1 — Foundation (V1) | `v1/` | F01–F16 | 16 |
| Phase 2 — Depth (V2) | `v2/` | F17–F26 | 10 |
| Phase 3 — Scale (V3) | `v3/` | F27–F35 | 9 |
| Cross-cutting platform modules | `cross-cutting/` | F36–F39 | 4 |
| **Total** | | | **39** |

Each `v1/v2/v3` slice corresponds to exactly one bullet in the matching
[FORGE_SPEC Phased Roadmap](../FORGE_SPEC.md) section. The cross-cutting four are extracted
because the roadmap consumes them implicitly (e.g. "human approval before merge",
"audit log for every action", "BYOK") rather than listing them as standalone checkboxes.

---

## Relationship to the overnight V1 build

[`docs/superpowers/plans/2026-06-26-forge-v1-overnight.md`](../superpowers/plans/2026-06-26-forge-v1-overnight.md)
is the **overnight V1 build plan**: a single execution plan for a Workflow swarm to stand up
the entire V1 monorepo in one run (Phase 0 architect → Phase 1 parallel fan-out → Phase 2
integration → Phase 3 morning report).

The two artifacts are complementary, not redundant:

- **The overnight plan is task-oriented and time-boxed.** It is optimized for one swarm run:
  one subagent per package, editing only its own directory, against frozen Phase-0 contracts.
  It covers **V1 only** (V2/V3 are explicitly out of scope).
- **Implementation slices are feature-oriented and durable.** They cover **all phases**
  (V1+V2+V3+cross-cutting), survive past the overnight run, and are what an engineer reads to
  build or extend a single feature later — including features the overnight plan never touches.

### Phase 0 of the overnight plan *is* the F00 foundation substrate

The overnight plan's **Phase 0 (Architect)** — tasks 0.1–0.6 — builds exactly the substrate
that the slices reference as `F00`: the uv/pnpm workspace (0.1), `packages/db` data model +
Alembic baseline (0.2), `packages/contracts` frozen DTOs/Protocols (0.3), `apps/api` router
skeleton (0.4), `apps/web` shell (0.5), and deploy/CI/test infra (0.6). Until a dedicated
`C01` slice exists, **read overnight-plan Phase 0 wherever a slice says it depends on F00.**

### Overnight Phase-1 task → slice mapping

The overnight plan's Phase-1 tasks build the V1 features by package. Each maps to its slice(s):

| Overnight task | Package / app | Slice(s) |
|---|---|---|
| 1.1–1.4 | `knowledge-core` + `evaluation` (retrieval golden set) | F05 (+ F12 retrieval eval) |
| 1.5 / 1.6 | `board-core` / `apps/web` board UI | F01 |
| 1.7 | `spec-engine` | F02 |
| 1.8 | `workflow-engine` | F07 |
| 1.9 | `agent-runtime` | F06 |
| 1.10 | `policy-sdk` | F04 |
| 1.11 | `skill-sdk` | F11 |
| 1.12 | `mcp-sdk` + `apps/mcp-gateway` | F09 |
| 1.13 | `integration-sdk` (GitHub + Slack) | F03 + F16 |
| 1.14 | observability + audit + trace assembler | F38 + F39 + F10 |
| 1.15 | auth & secrets (BYOK vault, RBAC) | F37 |
| 1.16 | `evaluation` golden task set + harness | F12 |
| 1.17 | examples + `docs/self-hosting/` | F15 (+ F13) |
| 0.6 | `deploy/docker-compose*` | F14 |
| (composed: 1.8+1.9+1.13+1.14) | plan→execute→verify→PR→approval flow | F08 + F36 |

The slices are the precise, per-feature expansion of these tasks; the overnight plan is the
schedule that builds the V1 subset of them in one night.

---

## How an engineer uses one slice to drive implementation

1. **Read the spec context first.** Open the slice, then read the FORGE_SPEC sections it
   cites in §1 and §11. The slice assumes the spec is the contract.
2. **Resolve dependencies (§5).** Confirm each REQUIRED upstream slice is built. If one is
   only "stubbable/soft", use the in-package stub the slice describes so you can build standalone.
3. **Freeze the contracts (§4).** Implement the Pydantic models / `Protocol`s / schemas
   exactly as written — other slices depend on these signatures. Do not drift them.
4. **Drive with TDD (§7 → §6).** Write the failing tests from the test plan first, mapping
   each test to a numbered acceptance criterion in §6. Implement until green. Follow
   `superpowers:test-driven-development`.
5. **Touch only the listed files (§10).** Create/edit exactly the paths in §10 (plus the
   pre-stubbed API router handler and worker task the slice names). Stay inside the feature's
   package boundary.
6. **Honor the non-negotiables (§8).** Spec-gated implementation, human approval before merge,
   read-only MCP default, BYOK, secret redaction, and immutable audit logging are enforced,
   not optional.
7. **Verify before claiming done.** Green gate = `ruff check` + type check + `pytest` on the
   slice's scope, with every §6 AC backed by a passing §7 test. Use
   `superpowers:verification-before-completion`. Defer anything in §12 — do not scope-creep.

A slice is implemented correctly when every acceptance criterion in §6 has a passing test
from §7, the §4 contracts are unchanged, and the §8 considerations are demonstrably met.
