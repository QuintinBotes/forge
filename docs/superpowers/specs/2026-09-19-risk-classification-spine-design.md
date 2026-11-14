# A — Risk classification spine

**Date:** 2026-09-19
**Status:** Approved, implementation-ready
**Depends on:** nothing (F is required only to dogfood this, not to build it)
**Blocks:** B, C, D, E

---

## 1. Problem

Forge treats every change alike. The deploy gate keys off **environment** — `environments` and
`restricted_environments` in `.forge/policy.yaml` — never off how dangerous the change itself is.
A README typo bound for production and a rewrite of a payment path bound for production take
identical routes at identical speed.

`risk.class|RiskClass|risk_class` returns **0 files** across the repository.

The reference model is blunt about the cost:

> Treating all changes alike forces the whole estate to move at the speed of its most dangerous
> change. […] This is the single highest-leverage structural decision in a regulated setting, and
> it is usually made implicitly and badly.

Forge currently makes it implicitly. This sub-project makes it explicit, derived, and auditable.

## 2. Approach

A new `risk-core` package computes a risk class from the diff. `.forge/policy.yaml` gains a
`risk_rules:` block. Two append-only tables record every classification and every gate
observation. The five shipping gates gain an optional `RiskReader` seam.

Two alternatives were considered and rejected:

- **Extend `orchestration-policy`**, where `BlastRadiusLevel` already lives. Rejected: that module
  sizes *how much agent to apply* (junior/medior/senior, single/swarm). Risk class governs *how
  much scrutiny before shipping*. Same signals, different consumers, different retune cadence —
  coupling them means tuning agent sizing silently retunes merge gates.
- **Fold into `policy-sdk`'s conditional evaluator**, reusing its rule engine and
  `policy_rule_evaluation` table. Rejected: `policy-sdk` answers *may this actor perform this
  action* — authorization. Risk class answers *how dangerous is this change* — classification.
  Conflating them makes every risk-threshold retune an authorization change, reviewed by the
  wrong people.

The signal **vocabulary** from `complexity.py` is reused. Its **thresholds** are not.

## 3. Risk model

### 3.1 Derived floor, declared escalation

```
derived_class = max(class of every matching rule, policy default)   # the floor
final_class   = max(derived_class, declared_class)                  # escalation is free
```

Declaring *below* the derived floor is de-escalation. It requires a `RiskOverride` carrying a
named approver and a reason, both recorded. Without one the change is **refused**, not silently
downgraded.

This ordering matters because an agent authoring its own spec would otherwise be classifying its
own risk — precisely the "implicitly and badly" failure. Derivation reads the diff; the diff does
not have opinions.

### 3.2 Classes

`RiskClass` is an ordered enum: `minimal < low < high < critical`.

```yaml
risk_rules:
  enforcement: shadow          # shadow | enforcing
  default: low
  fallback: critical           # used when signals are unavailable
  derive:
    - paths: ["docs/**", "**/*.md"]        class: minimal
    - paths: ["alembic/versions/**"]       class: high
    - paths: ["app/auth/**"]               class: high
    - paths: ["app/billing/**"]            class: critical
    - paths: ["infra/prod/**"]             class: critical
  classes:
    minimal:  { approvals: 0, deploy: auto,   red_team: false, self_eval: false }
    low:      { approvals: 1, deploy: auto,   red_team: false, self_eval: true  }
    high:     { approvals: 2, deploy: gated,  red_team: false, self_eval: true  }
    critical: { approvals: 2, deploy: gated,  red_team: true,  self_eval: true  }
```

Glob semantics reuse the existing `ConditionOp.MATCHES_GLOB` implementation (tested at
`packages/contracts/tests/test_conditions.py:89`) so risk rules and policy rules cannot disagree
about what `infra/**` means.

Absent a `risk_rules:` block nothing is computed, nothing is written, and behaviour is
bit-identical to today.

## 4. Two load-bearing invariants

### 4.1 Enforcement is monotone-tightening

Risk may add approvals, demand red-team, force a gated deploy. It may **never** make any gate more
permissive than it is today.

```
final_decision = tighten(existing_decision, risk_requirements)
```

A misconfigured `risk_rules:` block therefore cannot open a hole. Its worst case is over-blocking
— visible, annoying, and quickly corrected — rather than a silent widening nobody notices. This is
asserted by test, not merely intended.

### 4.2 Unavailable signals fail closed

If the diff cannot be read, the change takes `fallback` (default `critical`) and the reason is
recorded. This matches the repository's existing deny-by-default posture. Silently passing an
unclassifiable change would make the classifier worse than useless: it would be a control that
disengages exactly when it cannot see.

## 5. Components

### 5.1 `packages/risk-core/forge_risk/`

Pure and I/O-free, in the style of `orchestration-policy/complexity.py`.

| Module | Responsibility |
|---|---|
| `signals.py` | `ChangeSignals` — facts from a diff: paths touched, migration present, contract/schema touch, LOC added/removed, repo count. Data only |
| `rules.py` | `RiskRules` parsing and validation for the policy block |
| `classifier.py` | `derive(signals, rules) -> DerivedRisk`. Pure, total, deterministic |
| `evaluation.py` | `evaluate(derived, declared, override) -> RiskEvaluation`. Floor logic and override handling |
| `requirements.py` | `requirements_for(risk_class, rules) -> GateRequirements` |
| `errors.py` | Typed errors, raised only at policy-load time, never during evaluation |

### 5.2 `forge_contracts`

New: `RiskClass`, `RiskRules`, `RiskClassRequirements`, `ChangeSignals`, `DerivedRisk`,
`RiskEvaluation`, `RiskOverride`, `GateRequirements`. `Policy` gains an optional `risk_rules`
field — optional so every existing policy file stays valid.

### 5.3 Database

Migration `0041_risk_classification.py` (0040 is the current head). Two tables, both
`WorkspaceScopedModel`, both opting into `attach_immutability_trigger` (`forge_db/base.py:78`)
following the `policy_rule_evaluation` precedent.

**`risk_classification`** — one row per classified change:

| Column | Notes |
|---|---|
| `id`, `workspace_id` | |
| `commit_sha`, `pr_numbers`, `agent_run_id`, `workflow_run_id`, `spec_key`, `spec_version` | changeset identity, mirroring `attestation`'s field set |
| `derived_class`, `declared_class`, `final_class` | |
| `matched_rule_ids` | JSON, ordered |
| `signals` | JSON, redacted snapshot |
| `override_approver`, `override_reason` | nullable |
| `enforcement_mode` | `shadow` \| `enforcing` — what was in force at the time |
| `fallback_applied` | bool, true when signals were unavailable |
| `created_at` | |

**`risk_gate_observation`** — one row per gate evaluation:

| Column | Notes |
|---|---|
| `id`, `workspace_id`, `classification_id` | FK to the above |
| `gate_name` | `deploy` \| `merge` \| `approval` \| `red_team` \| `self_eval` |
| `actual_decision` | what the gate really returned |
| `risk_decision` | what risk requirements would have produced |
| `agreed` | bool |
| `created_at` | |

The second table is what makes the shadow phase produce **evidence** rather than confidence.
Agreement rate per gate is a query, and it is the stated criterion for flipping enforcement.

### 5.4 Gate integration

The five shipping gates:

| Gate | Location |
|---|---|
| deploy | `packages/deploy-core/forge_deploy/gate.py` |
| merge | `packages/workflow-engine/forge_workflow/multi_repo/merge.py` |
| approval | `packages/approval-sdk/forge_approval/service.py`, `authorizer.py` |
| red-team | `packages/db/forge_db/redteam/repository.py` and its caller |
| self-eval | `packages/evaluation/forge_eval/sweval/gate.py` |

(`spec-engine/gates.py` and `agent-runtime/policy_gate.py` are *not* in scope: the former
validates specs, the latter authorizes agent actions. Neither gates shipping.)

Each gains an optional `risk: RiskReader | None = None` dependency. `RiskReader` is a `Protocol`
in `forge_contracts`, so no gate package takes a dependency on `risk-core`.

- **Shadow:** the gate computes its decision exactly as today, asks risk what it *would* decide,
  writes a `risk_gate_observation`, and returns its own decision **unchanged**.
- **Enforcing:** the gate composes risk requirements into its decision under §4.1's
  tighten-only rule.

`risk=None` restores today's code path exactly.

## 6. Data flow

```
diff ──▶ ChangeSignals ──▶ derive() ──▶ DerivedRisk (floor)
                                            │
      spec-declared class ──────────────────┤
                                            ▼
                                     evaluate() ──▶ RiskEvaluation
                                            │              │
                    declared < floor?  ─────┤              ├──▶ risk_classification
                    needs named approver    │              │      (append-only)
                    else REFUSED            │              ▼
                                            │      GateRequirements
                                            ▼              │
                         ┌──────────────────┴──────────────┴─────────────────┐
                         ▼            ▼            ▼            ▼            ▼
                      deploy       merge      approval     red-team     self-eval
                         │            │            │            │            │
              shadow:  record what each WOULD decide, return actual unchanged
              enforce: tighten-only composition with actual
                         └──────────────────────────────────────────────────▶ risk_gate_observation
```

## 7. API and UI

- `GET /api/risk/classifications` — filterable by changeset identity
- `GET /api/risk/agreement` — per-gate agreement rate over a window; the evidence for flipping
- `POST /api/risk/classify` — classify a diff without recording (dry run, for authoring rules)

UI: a Risk panel in Settings showing the active rules, current enforcement mode per repo, and the
agreement rate per gate, with the flip control disabled until an agreement threshold is met. The
threshold is advisory and shown, not secretly enforced.

## 8. Error handling

The classifier is **total**: it never raises. Malformed rules are rejected at policy-load time by
the existing `forge_policy.load_policy` error path, which already has typed errors and tests.

| Condition | Behaviour |
|---|---|
| Unknown class name in policy | Load-time validation error; policy rejected |
| No rule matches | `default` class |
| Diff unavailable | `fallback` class, `fallback_applied=true`, reason recorded |
| Declared below floor, no override | Refused with a typed error naming the floor and the declaration |
| Declared below floor, override present | Allowed, override approver and reason recorded |
| Risk subsystem itself errors in shadow mode | Logged, observation skipped, gate decision unaffected |

That last row matters: in shadow mode a failure in the risk subsystem must never affect a real
gate outcome. Shadow means shadow.

## 9. Testing

In priority order.

1. **Shadow-mode equivalence.** The entire existing gate test matrix re-run with risk enabled,
   asserting every decision is unchanged. This is the test that lets A ship without endangering a
   working system, and it is written first.
2. **Property tests.** *Monotonicity*: adding a matching rule never lowers a class.
   *Totality*: `derive()` never raises across generated inputs.
3. **Enforcement monotonicity.** For every combination of existing decision and risk requirement,
   the enforcing outcome is never more permissive than the shadow outcome.
4. **Override semantics.** De-escalation without an override is refused; with one it is allowed
   and both approver and reason are persisted.
5. **Fail-closed.** Unavailable signals yield `fallback`, never `default`.
6. **Immutability.** UPDATE and DELETE rejected at the database layer on both tables, following
   the `policy_rule_evaluation` precedent.

Repo-unique test basenames (whole-repo `prepend` collection; a duplicate basename fails collection
with exit 2 across unrelated jobs): `test_risk_classifier.py`, `test_risk_evaluation.py`,
`test_risk_rules_loading.py`, `test_risk_gate_shadow.py`, `test_risk_gate_enforcing.py`,
`test_risk_tables_immutable.py`.

Budget ~20 minutes for `pytest packages/db/tests`; it applies every migration.

## 10. Acceptance criteria

1. A repo with no `risk_rules:` block behaves bit-identically to today, asserted by the full
   existing gate suite passing unchanged.
2. With rules in shadow mode, every change is classified and every gate evaluation recorded, and
   no gate decision differs from the unclassified run.
3. `GET /api/risk/agreement` returns a real per-gate agreement rate computed from observations.
4. Flipping one repo to `enforcing` changes gate outcomes only in the tightening direction.
5. A change touching a `critical` path cannot be shipped as `low` without a recorded named
   approver.
6. An unreadable diff yields `critical`, not `low`.
7. Both tables reject UPDATE and DELETE on Postgres.
8. `make lint`, `make typecheck`, and the full suite pass.

## 11. What A deliberately does not do

- **It does not change any gate's decision on delivery.** Shadow mode is the shipped default.
- **It does not classify by author, team, or agent-vs-human.** Risk is a property of the change.
  Measuring the individual is out of bounds for this whole programme.
- **It does not implement canary, rings, or flags.** `GateRequirements.deploy_mode` is produced
  and recorded; B consumes it.
- **It does not ship default rules for existing repos.** Rules are authored per repo, opt-in.
  A repo-wide default would change behaviour everywhere at once on a pre-1.0 system.
