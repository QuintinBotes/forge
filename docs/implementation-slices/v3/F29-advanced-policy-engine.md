# F29 — Advanced Policy Engine (conditional rules)

> Phase: v3 · Spec module(s): Repo Policy Layer (`packages/policy-sdk`), Security §"Policy evaluation" + §"Audit log", Workflow Engine §`escalation_policy`/`on_policy_conflict`, Human Approval System §"Policy override" / §"PR approval" · Status target: **Done** = `.forge/policy.yaml` gains an optional, schema-versioned `rules:` block of declarative **conditional rules**; a deterministic `ConditionalPolicyEvaluator` composes the flat F04 decision with the matched conditional rules under a documented, fail-closed precedence ladder that can **never** loosen a critical base denial **nor the human-approval-before-merge gate**; the agent runtime supplies a `PolicyContext` (branch, environment, task kind, actor role, skill profile, clock) on every tool call; every conditional decision is recorded in an append-only `policy_rule_evaluation` audit row, emitted as a `policy.decision` event to the central immutable audit log (F39), and surfaced in F36's approval/risk UI; a conditional `require_approval` raises an F36 `policy_override` gate (never self-granted); repos can ship `.forge/policy.tests.yaml` assertions runnable via `forge policy test` and `POST /policy/repos/{id}/simulate`; and a `schema_version: 1` policy evaluates byte-for-byte identically to F04 (regression-locked).

---

## 1. Intent — what & why

F04 (Repo Policy System) ships a **flat** policy: `write_rules`, `deploy_rules`, `subagent_rules`, etc. are evaluated context-free — the decision for `write_file app/x.py` is the same regardless of which branch, which environment, who initiated the run, what skill profile is active, or what day it is. The FORGE_SPEC.md Phase 3 roadmap explicitly calls for an **"Advanced policy engine with conditional rules"**, and F04's own §12 defers exactly this: *"Conditional/advanced policy engine — rule expressions, time/branch-conditional rules, per-environment matrices. V1 is flat declarative rules."*

F29 adds a **conditional refinement layer** on top of the flat base, without rewriting F04. Concretely it lets a repo express rules such as:

- *Require human approval for `deploy` to `production` outside business hours* (time-conditional; the canonical `deploy-prod-business-hours-only` rule in §4).
- *Require human approval for any write to `alembic/versions/**` when `task_kind == feature`* (path + task-conditional escalation).
- *Allow `run_command terraform apply` only when `environment == dev` and `actor_role == admin`* (bounded loosening of a base `run_command` denial).
- *Deny `spawn_subagent role=implementer` unless `execution_mode == supervised_multi_agent`* (mode-conditional).
- *Require approval for any write to `infra/**` on `branch == main`* (branch-conditional).

The design is governed by three non-negotiables drawn from the spec's Build Prompt constraints (#2 "the agent never self-assigns permissions or expands its own scope") and Security (#"Every tool invocation checked against repo policy before execution"):

1. **Deterministic, no LLM.** Conditional rules are pure boolean predicate evaluation against a whitelisted context — identical in spirit to the deterministic Supervisor (multi-agent) and the F21 automation rule engine. No model call, ever.
2. **Tighten freely, loosen only explicitly and never past the critical floor.** A conditional rule can always `deny` or escalate to `require_approval`. It can only `allow` (loosen) a **non-critical** base denial, and only when the rule sets `override_base: true` — path-traversal, secret-file, and unknown-action denials from F04 are an immutable floor that conditional rules can never override.
3. **Fully auditable.** Every conditional decision records which rules matched, the redacted context, the base effect, and the final effect in an append-only `policy_rule_evaluation` row linked to the `AgentRun`/`Step`, feeding the approval UI's "Risks flagged" panel.

Without F29, deployment gates (`v3/F31-deployment-gates`), supervised multi-agent scoping (`v3/F27-supervised-multi-agent`), and per-environment promotion matrices have no policy primitive to express conditional intent and must be hard-coded.

---

## 2. User-facing behavior / journeys

**Repo maintainer (human).**
1. Bumps `.forge/policy.yaml` to `schema_version: 2` and adds a `rules:` list of conditional rules (full schema in §4).
2. Runs `forge policy lint .` — the linter validates each rule (condition fields against the whitelist, `applies_to` against known action names, unique `id`s) and warns on dangerous shapes (an `override_base: true` allow targeting a secret-like path; a time-conditional rule with no `now`-supplying runtime note).
3. Adds `.forge/policy.tests.yaml` (policy-as-code tests): `{context, tool_call, expect}` triples. `forge policy test .` runs them and exits non-zero on any mismatch, so policy changes are TDD-gated in the repo's own CI.
4. Opens **Project → Repo → Policy** and sees the conditional rules rendered read-only (each rule's `when` tree, `applies_to`, effect, severity), plus an interactive **Rule Simulator**: pick an action, fill a context (branch/env/task kind/role/time), and see the resulting `Decision` with the exact matched rules and the base-vs-final effect.

**Agent runtime (machine), during a run.**
1. On each tool dispatch the tool gate builds a `PolicyContext` from the run (branch, base branch, target environment, `task.kind`, the run-initiator's RBAC role, active skill profile, execution mode, UTC clock) and calls `evaluator.evaluate_in_context(tool_call, policy, context)`.
2. The evaluator returns a composed `Decision`. A conditional `deny` aborts the call and records a denied `Step`; a conditional `require_approval` (or base-allow escalated to a gate) **pauses** the call (it is never executed) and opens a `policy_override` gate via the canonical approval primitive owned by `cross-cutting/F36-human-approval-system` (`ApprovalService.create(gate_type="policy_override", ...)`). On admin approval F36 mints a single-use, short-TTL `PolicyOverrideGrant` bound to the exact action fingerprint and the paused call resumes once; on reject the call is denied and the run routes to `needs_human_input`. The agent never self-grants (Build Prompt #2).
3. Every evaluation that involved at least one conditional rule writes a `policy_rule_evaluation` audit row **and** emits a compact `policy.decision` `AuditEvent` through the canonical `AuditSink` (`cross-cutting/F39-audit-log`).

**Reviewer / admin (human).**
1. In F36's unified Approval UI (the `policy_override` gate review shell), the "Risks flagged" panel (must-show item 7) lists conditional matches: *"Rule `deploy-prod-business-hours-only` (critical) escalated this deploy to approval: outside 09:00–17:00 UTC."*
2. Audits, for any past `Step`, the exact context and matched rules that produced a decision (append-only, queryable).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The conditional `rules:` block lives **inside** the policy body, so F04's existing `RepoPolicySnapshot.policy_json` (and `PolicyProfile.body`) already persist it — no change to those columns; the JSON simply now validates against `Policy` `schema_version: 2`. F29 adds **one** append-only audit table.

**New table — `PolicyRuleEvaluation` (`packages/db/forge_db/models/policy_rule_evaluation.py`):** immutable record of one conditional evaluation that matched ≥1 rule.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `agent_run_id` | UUID FK → `agent_run.id` null | null for dry-run/simulate calls |
| `step_id` | UUID FK → `step.id` null | the tool-call step this gated (null for simulate) |
| `policy_snapshot_id` | UUID FK → `repo_policy_snapshot.id` null | governing policy version (F04) |
| `action` | text | `ToolCall.name` |
| `base_effect` | text | `allow` \| `deny` (flat F04 decision) |
| `final_effect` | text | `allow` \| `deny` |
| `requires_approval` | bool | final gate flag |
| `severity` | text | `info` \| `warning` \| `critical` |
| `matched_rule_ids` | JSONB | ordered `["rule-id", ...]` that fired |
| `context_redacted` | JSONB | `PolicyContext.to_redacted_fields()` (no command/args bodies, no secrets) |
| `evaluated_at` | timestamptz | |

- Index on `(agent_run_id)`, `(workspace_id, evaluated_at)`.
- Append-only at **two layers**: the service exposes no UPDATE/DELETE path, **and** the table opts into the reusable `attach_immutability_trigger("policy_rule_evaluation")` Postgres trigger helper from `cross-cutting/F39-audit-log` (DB-level rejection of UPDATE/DELETE). This is the conditional-layer extension of F04's "auditability of the governing policy".
- **Dual-write to the central audit log.** `policy_rule_evaluation` is the rich, queryable per-decision record; in the same transaction the service also emits a compact `AuditEvent(action="policy.decision", ...)` through the canonical `AuditSink` (`cross-cutting/F39-audit-log`), so conditional policy decisions land in the platform's single immutable, hash-chained `audit_log` (Security §"Audit log" / Build Prompt #9). The `AuditEvent` payload is the redacted projection only (never raw `ToolCall.args`), redacted by F39's `SqlAuditWriter` before persistence.
- We do **not** add a row when zero conditional rules match (the flat F04 decision already covers that, and the F10 run trace records the `Step`); F29 audit rows exist only when the conditional layer changed or contributed to the outcome.

**Migration:** one Alembic revision `xxxx_f29_policy_rule_evaluation` — `CREATE TABLE policy_rule_evaluation` (+ the two indexes + `attach_immutability_trigger`). Additive, backward-compatible, depends on the F04 `repo_policy_snapshot` migration, the F07/F10 `agent_run`/`step` migrations, and the `cross-cutting/F39-audit-log` migration (immutability-trigger helper + `audit_log`).

### 3.2 Backend (FastAPI routes + services/packages)

**Shared primitive `packages/contracts/forge_contracts/conditions.py` (NEW):** the generic condition DSL lifted to contracts so policy-sdk and (future) the F21 automation engine share one implementation:

- `ConditionOp` (StrEnum), `Condition`, `ConditionGroup`, `evaluate_condition(group, fields, *, field_whitelist)` — pure boolean evaluation (signatures in §4). policy-sdk and automation pass their own `field_whitelist`; F21 may migrate onto this primitive (out of scope here — see §12).

**Package `packages/policy-sdk/forge_policy/` (extended; still no FastAPI imports):**

- `schema.py` — add `RuleEffect`, `ConditionalRule`, and the `rules` field + `schema_version` cross-validator to `Policy`; add `conditional_matches` + `base_effect` to `Decision` (additive). Bump the re-exported `forge_contracts.policy` models in lock-step.
- `context.py` (NEW) — `PolicyContext` model + `to_fields()` / `to_redacted_fields()`; `POLICY_CONDITION_FIELDS` whitelist; `build_context_from_run(...)` helper signature (the runtime's contract).
- `conditional.py` (NEW) — `ConditionalPolicyEvaluator` implementing the precedence ladder (§4); wraps the F04 `DefaultPolicyEvaluator` as the base layer.
- `matching.py` — reuse F04's `pathspec` gitwildmatch for the `matches_glob` op (path/branch globs).
- `tests_runner.py` (NEW) — `PolicyTestSuite` loader for `.forge/policy.tests.yaml` + `run_policy_tests(policy, suite) -> PolicyTestReport`.
- `errors.py` — add `PolicyRuleError` (subclass of F04 `PolicyValidationError`) for rule-specific validation failures.

**Service `apps/api/forge_api/services/policy_service.py` (extended):**

- `evaluate_tool_call(repo_connection_id, ref, tool_call, context) -> Decision` — F04's signature gains an optional `context: PolicyContext`; persists a `PolicyRuleEvaluation` row **and** emits a `policy.decision` `AuditEvent` via the injected `AuditSink` (F39) when conditional rules contribute and a run/step id is supplied. The `AuditSink` is constructor-injected so the service is unit-testable with an in-memory sink.
- `simulate(repo_connection_id, ref, tool_call, context) -> SimulationResult` — pure dry-run (no persistence); returns the `Decision` plus a per-rule `RuleTrace` (matched/not-matched + why). Powers the UI simulator and CLI.
- `run_policy_tests(repo_connection_id, ref) -> PolicyTestReport` — fetch `.forge/policy.tests.yaml` via integration-sdk and run it against the resolved policy.
- `create_profile/update_profile` — `body` now revalidates against `Policy` `schema_version: 2` (rules included).

**Router `apps/api/forge_api/routers/policy.py` (extended; auth + RBAC from foundation as in F04):**

| Method & path | Body → Response | Purpose |
|---|---|---|
| `POST /policy/repos/{repo_connection_id}/simulate` | `SimulateRequest{tool_call, context, ref?}` → `SimulationResult` | dry-run a decision with full rule trace (admin/agent-runner) |
| `POST /policy/repos/{repo_connection_id}/test` | `{ref?}` → `PolicyTestReport` | run `.forge/policy.tests.yaml` (admin) |
| `GET /policy/repos/{repo_connection_id}/rule-evaluations` | query: `agent_run_id?`, `limit` → `list[PolicyRuleEvaluationOut]` | audit query (admin/member) |

F04's existing `POST /policy/repos/{id}/evaluate` is extended to accept an optional `context`; `GET /policy/repos/{id}` (`EffectivePolicyResponse`) now includes the rendered `rules`.

**CLI `apps/api/forge_api/cli/policy.py` (extended):**
- `forge policy simulate <repo_or_path> --action write_file --path app/x.py --env dev --branch main --task-kind feature [...]` — prints the `Decision` + matched rules (works against a local worktree path or a connected repo id).
- `forge policy test <path>` — runs `.forge/policy.tests.yaml`; exit 0/1 (the repo-CI hook).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

- **Agent-runtime tool gate (consumer; contract defined here, wired in F06/F08).** The tool node, instead of F04's `evaluator.evaluate(tool_call, policy)`, calls `evaluator.evaluate_in_context(tool_call, policy, context)` where `context = build_context_from_run(run, task, repo_target, clock=utcnow)`. The runtime is **required** to populate `now` (UTC), `branch`, `base_branch`, `environment` (when the action targets one), `task_kind`, `actor_role` (run initiator's RBAC role), `skill_profile`, and `execution_mode`. On a `deny` it records a denied `Step`; on `requires_approval=True` it **pauses** the call and opens a `policy_override` gate through F36's `ApprovalService.create(...)` (registering a `GateContextProvider` that supplies the attempted action, the matched rule(s), severity, and rationale — see §3.4); whenever ≥1 conditional rule fired it persists a `PolicyRuleEvaluation` row and emits the `policy.decision` `AuditEvent` (F39).
- **No new Celery task and no new LangGraph node.** Like F04, F29 provides a synchronous, pure `evaluate_in_context()` the existing tool node calls. The `refresh_repo_policy_snapshot` task (F04) already re-resolves the snapshot on policy file pushes — `schema_version: 2` bodies flow through unchanged.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Extend the F04 policy route `apps/web/app/(board)/projects/[projectId]/repos/[repoId]/policy/page.tsx` and add components under `apps/web/components/policy/`:

- `ConditionalRulesView` — read-only list of `rules`: each card renders `applies_to`, the `when` tree (nested all/any groups), effect badge (allow/deny/require-approval), severity, and `override_base` flag.
- `RuleSimulator` — form (action select + context fields: branch, environment, task kind, actor role, skill profile, datetime) → `POST /policy/repos/{id}/simulate`; shows the final `Decision`, base vs final effect, and the ordered matched-rule trace (matched rules highlighted, with the predicate that fired).
- `PolicyTestPanel` (admin) — runs `POST /policy/repos/{id}/test`, renders pass/fail per assertion.
- Approval-UI extension: F29 registers a `policy_override` `GateContextProvider` with F36 (`cross-cutting/F36-human-approval-system`) that projects `decision.conditional_matches` into the gate's must-show payload; F36's unified Approval UI renders them under "Risks flagged" (must-show item 7: rule id, severity, reason). F29 adds no approval table or UI of its own — it plugs into F36's frame.

Hooks: `useSimulatePolicy(repoId)`, `usePolicyTests(repoId)`, `useRuleEvaluations(repoId, agentRunId)` (TanStack Query).

### 3.5 Infra / deploy (compose, helm, caddy, if any)

N/A for runtime infra — no new service/container. F29 ships repo artifacts:

- **`examples/policies/conditional/*.yaml`** — ≥4 worked conditional policies (time-gated deploy, branch-gated infra writes, env+role-gated `run_command`, mode-gated subagents) + a matching `*.tests.yaml` for each. A CI gate (`pytest packages/policy-sdk/tests/test_examples_conditional.py`) loads each through `load_policy` (`is_valid`) and runs its test suite green.
- **`packages/policy-sdk/forge_policy/policy.schema.json`** — regenerated from the `schema_version: 2` `Policy.model_json_schema()`; the F04 drift-guard test now covers the `rules` block.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Shared condition DSL (`forge_contracts/conditions.py`):**

```python
from enum import StrEnum
from typing import Any, Literal, Mapping
from pydantic import BaseModel, Field

class ConditionOp(StrEnum):
    EQ = "eq"; NE = "ne"; IN = "in"; NOT_IN = "not_in"
    LT = "lt"; LTE = "lte"; GT = "gt"; GTE = "gte"
    CONTAINS = "contains"; NOT_CONTAINS = "not_contains"
    IS_NULL = "is_null"; IS_NOT_NULL = "is_not_null"
    MATCHES_GLOB = "matches_glob"        # gitwildmatch (pathspec) for path/branch; value: str | list[str]
    IN_TIME_WINDOW = "in_time_window"        # operand field must be a datetime (e.g. `now`); value: {days:[0..6], start:"HH:MM", end:"HH:MM", tz:"UTC"}
    NOT_IN_TIME_WINDOW = "not_in_time_window"  # boolean inverse of IN_TIME_WINDOW (same value shape); used to gate "outside the window"

class Condition(BaseModel):
    field: str
    op: ConditionOp
    value: Any = None

class ConditionGroup(BaseModel):
    match: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(default_factory=list)
    groups: list["ConditionGroup"] = Field(default_factory=list)
    # empty group (no conditions, no groups) == always True

def evaluate_condition(
    group: ConditionGroup,
    fields: Mapping[str, Any],
    *,
    field_whitelist: frozenset[str],
) -> bool:
    """Pure boolean eval, no I/O. Raises ValueError if any Condition.field is not in
    field_whitelist, or on op/value mismatch (IN/NOT_IN without a list; IN_TIME_WINDOW/
    NOT_IN_TIME_WINDOW without the dict shape or on a non-datetime operand). A field
    missing from `fields` (or None, e.g. `now` unset) is treated as None: positive ops
    (EQ/IN/CONTAINS/MATCHES_GLOB/LT.../IN_TIME_WINDOW) -> False; IS_NULL -> True;
    IS_NOT_NULL/NE/NOT_IN/NOT_CONTAINS/NOT_IN_TIME_WINDOW -> True (so a missing clock
    fails CLOSED for an 'outside-the-window' gate). 'all' over an empty group -> True."""
```

**Policy context (`forge_policy/context.py`):**

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

POLICY_CONDITION_FIELDS: frozenset[str] = frozenset({
    "action", "path", "file_ext", "environment", "command", "role", "skill_profile",
    "branch", "base_branch", "task_kind", "actor_role", "execution_mode",
    "labels", "repo_id",
    "now", "weekday", "hour",   # now=UTC datetime (operand for *_time_window); weekday 0=Mon..6=Sun; hour 0..23 (UTC)
})

class PolicyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str | None = None
    command: str | None = None
    role: str | None = None
    skill_profile: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    task_kind: str | None = None       # feature|bug|chore|spike|incident|change_request|doc
    actor_role: str | None = None      # admin|member|viewer|agent-runner (run initiator)
    execution_mode: str | None = None  # single_agent|supervised_multi_agent
    labels: list[str] = Field(default_factory=list)
    repo_id: str | None = None
    now: datetime | None = None        # UTC eval clock; runtime MUST set this

    @classmethod
    def empty(cls) -> "PolicyContext": ...

    def to_fields(self, action: "ToolCall") -> dict[str, Any]:
        """Flatten to the POLICY_CONDITION_FIELDS namespace. Derives `action`=action.name,
        `path`=action.path, `file_ext` from path, `command`=action.args.get('command'),
        `environment`/`role`/`skill_profile` from args when not set explicitly, and
        exposes `now` (the raw UTC datetime, operand for IN_TIME_WINDOW/NOT_IN_TIME_WINDOW)
        plus `weekday`/`hour` derived from it (all None if `now` is None)."""

    def to_redacted_fields(self) -> dict[str, Any]:
        """Audit-safe projection for PolicyRuleEvaluation.context_redacted: drops `command`
        (may carry secrets) and truncates `path`; keeps branch/env/task_kind/role/etc and
        `now` (so a time-conditional decision is reproducible/replayable in the audit)."""

def build_context_from_run(*, branch: str, base_branch: str, environment: str | None,
                           task_kind: str, actor_role: str, skill_profile: str,
                           execution_mode: str, repo_id: str, now: datetime,
                           labels: list[str] | None = None) -> PolicyContext: ...
```

**Schema additions (`forge_policy/schema.py` / `forge_contracts/policy.py`):**

```python
class RuleEffect(StrEnum):
    ALLOW = "allow"; DENY = "deny"; REQUIRE_APPROVAL = "require_approval"

class ConditionalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=120)
    description: str | None = None
    applies_to: list[str] = Field(default_factory=lambda: ["*"])  # ToolCall.name(s) or "*"
    when: ConditionGroup = Field(default_factory=ConditionGroup)  # empty => always matches
    effect: RuleEffect
    severity: Literal["info", "warning", "critical"] = "warning"
    reason: str = Field(min_length=1)
    priority: int = 100            # lower evaluated/cited first; ties broken by declaration order
    override_base: bool = False    # only meaningful when effect=ALLOW: may loosen a NON-critical base deny
    enabled: bool = True

# Policy gains (everything else from F04 unchanged):
class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1        # rules require schema_version >= 2
    # ... all F04 fields ...
    rules: list[ConditionalRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_rules(self) -> "Policy":
        """rules non-empty => schema_version >= 2 (else ValueError). Rule ids unique.
        Each Condition.field in POLICY_CONDITION_FIELDS. applies_to entries in
        KNOWN_ACTIONS | {'*'}. (Cross-field secret-floor warnings are emitted by the
        linter, not raised here.)"""

class ConditionalMatch(BaseModel):
    rule_id: str
    effect: RuleEffect
    severity: Literal["info", "warning", "critical"]
    reason: str

# Decision gains two additive, default-empty fields (F04 shape otherwise unchanged):
class Decision(BaseModel):
    effect: Literal["allow", "deny"]
    reason: str
    matched_rule: str | None = None
    requires_approval: bool = False
    severity: Literal["info", "warning", "critical"] = "info"
    conditional_matches: list[ConditionalMatch] = Field(default_factory=list)  # NEW
    base_effect: Literal["allow", "deny"] | None = None                        # NEW (flat layer)
    @property
    def allowed(self) -> bool: return self.effect == "allow"
```

**Evaluator (`forge_policy/conditional.py`) — extends, never replaces, F04:**

```python
class ConditionalPolicyEvaluator:   # satisfies forge_contracts.PolicyEvaluator
    def __init__(self, base: "PolicyEvaluator" | None = None) -> None:
        self._base = base or DefaultPolicyEvaluator()   # F04 flat evaluator

    def load(self, repo_root: Path) -> Policy: ...

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
        # Backward-compatible: F04 2-arg call == empty context.
        return self.evaluate_in_context(action, policy, PolicyContext.empty())

    def evaluate_in_context(self, action: ToolCall, policy: Policy,
                            context: PolicyContext) -> Decision: ...
```

**`evaluate_in_context()` precedence ladder (pure, total — every path returns a `Decision`):**

```
base = self._base.evaluate(action, policy)                      # F04 flat decision
fields = context.to_fields(action)
matched = [r for r in policy.rules
           if r.enabled
           and ("*" in r.applies_to or action.name in r.applies_to)
           and evaluate_condition(r.when, fields, field_whitelist=POLICY_CONDITION_FIELDS)]
matched.sort(key=lambda r: (r.priority, policy.rules.index(r)))
cm = [ConditionalMatch(rule_id=r.id, effect=r.effect, severity=r.severity, reason=r.reason)
      for r in matched]

# (1) IMMUTABLE FLOOR — conditional layer can tighten-cite but never loosen. Covers
#     (a) every CRITICAL base deny (path traversal, secret-file, disabled/restricted
#     deploy, unknown action) and (b) the PR-MERGE approval gate on merge/push, because
#     "human approval is required before PR merge — always" (Build Prompt #5) can never be
#     relaxed by policy. override_base allows are recorded in conditional_matches but ignored.
MERGE_GATE_ACTIONS = frozenset({"merge", "push"})
if base.effect == "deny" and (base.severity == "critical" or action.name in MERGE_GATE_ACTIONS):
    return Decision(effect="deny", reason=base.reason, matched_rule=base.matched_rule,
                    requires_approval=base.requires_approval, severity=base.severity,
                    conditional_matches=cm, base_effect="deny")

denies = [r for r in matched if r.effect == RuleEffect.DENY]
gates  = [r for r in matched if r.effect == RuleEffect.REQUIRE_APPROVAL]
allows = [r for r in matched if r.effect == RuleEffect.ALLOW]

# (2) Conditional DENY wins over everything below the floor.
if denies:
    r = denies[0]
    return Decision(effect="deny", reason=r.reason, matched_rule=f"rules[{r.id}]",
                    requires_approval=False, severity=r.severity,
                    conditional_matches=cm, base_effect=base.effect)

# (3) Non-critical base DENY: loosened ONLY by an override allow; else stands (with optional gate).
if base.effect == "deny":
    override = next((r for r in allows if r.override_base), None)
    if override is not None:
        if gates:
            return Decision(effect="deny", reason=gates[0].reason,
                            matched_rule=f"rules[{gates[0].id}]", requires_approval=True,
                            severity=gates[0].severity, conditional_matches=cm, base_effect="deny")
        return Decision(effect="allow", reason=override.reason,
                        matched_rule=f"rules[{override.id}]", requires_approval=False,
                        severity=override.severity, conditional_matches=cm, base_effect="deny")
    # no override: base deny stands; a gate cannot un-deny it, only annotate.
    return Decision(effect="deny", reason=base.reason, matched_rule=base.matched_rule,
                    requires_approval=base.requires_approval, severity=base.severity,
                    conditional_matches=cm, base_effect="deny")

# (4) Base ALLOW: a gate escalates to approval (effect=deny+requires_approval, per F04 convention).
if gates:
    r = gates[0]
    return Decision(effect="deny", reason=r.reason, matched_rule=f"rules[{r.id}]",
                    requires_approval=True, severity=r.severity,
                    conditional_matches=cm, base_effect="allow")
return Decision(effect="allow", reason=base.reason, matched_rule=base.matched_rule,
                requires_approval=False, severity=base.severity,
                conditional_matches=cm, base_effect="allow")
```

Key invariants encoded above: (a) `schema_version: 1` / no rules ⇒ `matched == []` ⇒ returns the F04 `base` verbatim plus empty `conditional_matches` (regression-locked — note branch (1) and branch (3)/(4) all return base's `effect`/`reason`/`matched_rule`/`requires_approval`/`severity` unchanged when no rules fire); (b) a critical base deny **and the PR-merge approval gate** are returned unchanged (never loosened); (c) loosening requires an explicit `override_base: true` allow on a non-critical, non-merge-gate deny; (d) a `require_approval` rule never executes a deny but never silently grants either.

**Policy-as-code tests (`forge_policy/tests_runner.py`) & `.forge/policy.tests.yaml`:**

```python
class PolicyTestCase(BaseModel):
    name: str
    context: PolicyContext = Field(default_factory=PolicyContext.empty)
    tool_call: ToolCall
    expect_effect: Literal["allow", "deny"]
    expect_requires_approval: bool | None = None
    expect_rule: str | None = None     # a rule id that must appear in conditional_matches

class PolicyTestSuite(BaseModel):
    cases: list[PolicyTestCase]

class PolicyTestReport(BaseModel):
    total: int; passed: int; failed: int
    failures: list[dict[str, Any]]     # [{name, expected, actual}]
    @property
    def ok(self) -> bool: return self.failed == 0

def run_policy_tests(policy: Policy, suite: PolicyTestSuite) -> PolicyTestReport: ...
```

```yaml
# .forge/policy.tests.yaml
cases:
  - name: prod deploy blocked outside business hours
    context: { environment: production, now: "2026-06-27T23:00:00Z" }   # Saturday night
    tool_call: { name: deploy, args: { environment: production } }
    expect_effect: deny
    expect_requires_approval: true
    expect_rule: deploy-prod-business-hours-only
```

**API request/response models (`apps/api/forge_api/schemas/policy.py`, additions):**

```python
class SimulateRequest(BaseModel): tool_call: ToolCall; context: PolicyContext = PolicyContext.empty(); ref: str | None = None
class RuleTrace(BaseModel): rule_id: str; matched: bool; effect: RuleEffect; reason: str
class SimulationResult(BaseModel): decision: Decision; base_effect: Literal["allow","deny"]; traces: list[RuleTrace]; commit_sha: str
class PolicyRuleEvaluationOut(BaseModel):
    id: str; action: str; base_effect: str; final_effect: str; requires_approval: bool
    severity: str; matched_rule_ids: list[str]; context_redacted: dict[str, Any]
    agent_run_id: str | None; step_id: str | None; evaluated_at: datetime
```

**Example conditional rule (canonical, used as the test golden):**

```yaml
schema_version: 2
repo_id: github.com/org/api
name: Core API Service
# ... F04 flat sections review_rules / knowledge_rules / skill_profiles / subagent_rules / commands unchanged ...
# review_rules.approval_required_for_merge stays true (the merge gate the conditional layer can never loosen — AC21).
# write_rules and deploy_rules are spelled out here so the conditional rules below have a meaningful flat base:
write_rules:
  # infra/** is ALLOWED at the flat layer so the infra-writes-main-only rule TIGHTENS it (base_effect=allow in AC4/AC5).
  allow: [app/**, tests/**, docs/**, alembic/versions/**, "infra/**"]
  # secret-like denies stay (critical) so an override_base allow can never reach them (AC8).
  deny: [".env*", "secrets/**", "*.pem", "*.key"]
deploy_rules:
  # production is PERMITTED at the flat layer so the conditional time-gate is the operative control;
  # a flat critical deny would otherwise hit the immutable floor and mask the gate (base_effect=allow in AC6).
  allow_agent_deploy: true
  environments: [dev, staging, production]
  restricted_environments: []
rules:
  - id: deploy-prod-business-hours-only
    applies_to: [deploy, promote_environment]
    when:
      match: all
      conditions:
        - { field: environment, op: eq, value: production }
        - { field: now, op: not_in_time_window, value: { days: [0,1,2,3,4], start: "09:00", end: "17:00", tz: UTC } }
    effect: require_approval        # production deploys OUTSIDE Mon-Fri 09:00-17:00 UTC escalate to human approval
    severity: warning
    reason: "Production deploys outside business hours require human approval."
  - id: infra-writes-main-only
    applies_to: [write_file, write_code, delete_file, move_file]
    when:
      conditions:
        - { field: path, op: matches_glob, value: "infra/**" }
        - { field: branch, op: ne, value: main }
    effect: deny
    severity: critical
    reason: "infra/** may only be modified on the main branch."
  - id: terraform-apply-dev-admin
    applies_to: [run_command]
    when:
      conditions:
        - { field: command, op: contains, value: "terraform apply" }
        - { field: environment, op: eq, value: dev }
        - { field: actor_role, op: eq, value: admin }
    effect: allow
    override_base: true             # loosens the base run_command allowlist denial (non-critical)
    severity: warning
    reason: "terraform apply allowed in dev for admins only."
```

---

## 5. Dependencies — features/slices that must exist first

> **Slug reconciliation (inherited from F04).** The platform substrate and the auth/secrets/RBAC concerns are cross-cutting prerequisites that every slice assumes. Sibling slices reference the substrate variously as `v1/F00-foundation-substrate` and `cross-cutting/F00-foundation`; the auth/secrets/RBAC + BYOK + `SecretRedactor` primitives are owned by `cross-cutting/F37-auth-secrets-byok`. `v1/F00-foundation-substrate` is used below as the substrate placeholder (matching F04, the slice F29 extends); the auth/RBAC primitives resolve to `cross-cutting/F37-auth-secrets-byok`.

- **`v1/F04-repo-policy`** — **REQUIRED, hard.** F29 imports `DefaultPolicyEvaluator`, the `Policy`/`ToolCall`/`Decision` models, `load_policy`, `matching.py` (pathspec), `RepoPolicySnapshot`, the `/policy/*` router + `PolicyService`, the CLI, and the web policy route — and extends each. The whole slice is an extension of F04.
- **`v1/F00-foundation-substrate`** — **REQUIRED** (Phase-0 `forge_contracts`, `forge_db` base session + `WorkspaceScopedModel`, `apps/api` skeleton, `apps/worker` Celery app, web app shell). Same baseline F04 depends on.
- **`cross-cutting/F37-auth-secrets-byok`** — **REQUIRED.** Provides the `Principal`/`get_principal` auth dependency and the flat `require_role(...)` RBAC dependency (roles admin/member/viewer/agent-runner) used to gate the new `/policy/*` routes, plus the canonical `SecretRedactor` used when building `context_redacted`. (BYOK is not exercised: F29 is deterministic and makes no model call — see §8.)
- **`cross-cutting/F39-audit-log`** — **REQUIRED for the audit path.** Provides the frozen `AuditEvent` contract + `AuditSink` Protocol + `SqlAuditWriter` (redacts before persist) that F29 emits `policy.decision` events through, and the reusable `attach_immutability_trigger(table)` helper the `policy_rule_evaluation` table opts into. The evaluator/schema/simulator are independently buildable; the persisted-decision path needs F39.
- **`cross-cutting/F36-human-approval-system`** — **REQUIRED for the approval surface.** Owns the canonical `policy_override` gate primitive (no other slice does), `ApprovalService.create(...)`, the `GateContextProvider`/`GateResolutionHook` registry, the single-use `PolicyOverrideGrant`, and the unified Approval UI that renders `conditional_matches` under "Risks flagged" (must-show item 7). F29 *consumes* this primitive; it does not build its own approval table or UI.
- **`v1/F06-single-execution-agent`** — **REQUIRED for the runtime wiring** (the tool gate that calls `evaluate_in_context` and supplies `PolicyContext` via `build_context_from_run`). F29 defines the context-builder contract; F06's tool node consumes it. The policy-sdk core + API + simulator are buildable/testable without F06.
- **`v1/F08-plan-execute-verify-pr-approval`** — **SOFT.** Provides the verify→PR→merge flow the gated actions live within; F29 does **not** depend on F08 for the `policy_override` gate (that is F36). F08 is relevant only as the surrounding execution context.
- **`v1/F10-run-trace-viewer`** — **SOFT** (surfaces `policy_rule_evaluation` rows alongside steps; read-only consumer, not needed to build F29).
- **`v1/F11-skill-profiles`** — **SOFT** (linter warning when a `skill_profile` value in a condition does not resolve to a registered profile; not a hard error so policies stay loadable without the registry).
- **`v2/F21-workflow-automations`** — **SOFT / sibling.** F29 lifts the `Condition`/`ConditionGroup`/`ConditionOp` shape into `forge_contracts.conditions`; F21 already ships an equivalent in `forge_automation`. F29 does not require F21; migrating F21 onto the shared primitive is future work (§12).
- **Downstream (NOT prerequisites; depend on F29):** `v3/F31-deployment-gates` (expresses promotion matrices as conditional `deploy`/`promote_environment` rules), `v3/F27-supervised-multi-agent` (mode/role-conditional `spawn_subagent` rules), `v3/F30-multi-team-rbac` (actor-role-conditional rules).

---

## 6. Acceptance criteria (numbered, testable)

1. **Backward compatibility (regression lock):** for a `schema_version: 1` policy (no `rules`), `ConditionalPolicyEvaluator.evaluate(action, policy)` returns a `Decision` equal to `DefaultPolicyEvaluator.evaluate(action, policy)` for the entire F04 `tool_call_matrix` (same `effect`, `reason`, `matched_rule`, `requires_approval`, `severity`); `conditional_matches == []`.
2. A policy with non-empty `rules` and `schema_version: 1` fails validation (`ValueError`/`PolicyValidationError`); `schema_version: 2` with the same rules validates.
3. Duplicate rule `id`s fail validation; a `Condition.field` not in `POLICY_CONDITION_FIELDS` fails validation; an `applies_to` entry that is neither a known action nor `*` fails validation.
4. **Conditional deny tightens a base allow:** rule `infra-writes-main-only` + `ToolCall(write_file, path="infra/x.tf")` with `context.branch="feature/x"` → `deny`, `severity="critical"`, `matched_rule="rules[infra-writes-main-only]"`, `base_effect="allow"`, and `conditional_matches[0].rule_id == "infra-writes-main-only"`.
5. The same rule on `context.branch="main"` → `allow` (condition `branch ne main` is False), `conditional_matches == []`.
6. **Gate escalates a base allow (time-conditional):** against a policy whose flat `deploy_rules` allow `production` (so `base_effect="allow"`), rule `deploy-prod-business-hours-only` + `ToolCall(deploy, args={environment:production})` with `context.now` **outside** the window (Saturday 23:00Z) → `effect="deny"`, `requires_approval=True`, `base_effect="allow"`, `matched_rule="rules[deploy-prod-business-hours-only]"`; the same call with `context.now` **inside** the window (Tuesday 12:00Z) → `effect="allow"` (rule does not fire), `conditional_matches == []`.
7. **Bounded loosening:** rule `terraform-apply-dev-admin` + `ToolCall(run_command, args={command:"terraform apply -auto-approve"})` with `context.environment="dev"`, `actor_role="admin"` → `allow` (overrides the base `run_command` allowlist deny), `base_effect="deny"`, `matched_rule="rules[terraform-apply-dev-admin]"`. With `actor_role="member"` → base deny stands (`deny`, `requires_approval=True`).
8. **Override cannot defeat the critical floor:** an `override_base: true` allow rule that matches a `write_file` to `secrets/x.pem` (F04 secret-file critical deny) → still `deny`, `severity="critical"`, `base_effect="deny"`; the allow is recorded in `conditional_matches` but does not change `effect`.
9. **Override cannot defeat path traversal:** any conditional allow for `ToolCall(write_file, path="../../etc/passwd")` → still `deny`, `matched_rule="path_traversal"`.
10. **Deny precedence among conditionals:** when both a matching `deny` rule and a matching `allow override` rule fire on a non-critical base deny, the result is `deny`.
11. **Priority ordering is deterministic:** two matching `deny` rules with `priority 10` and `priority 20` → `matched_rule` cites the `priority 10` rule; reversing declaration order does not change the result.
12. **Empty `when` always matches:** a rule with an empty `when` and `applies_to: ["*"]` fires for every action of any context.
13. **Time window correctness:** `in_time_window` with `days:[0..4], 09:00–17:00 UTC` is True for a Tuesday 12:00Z `now`, False for a Saturday 12:00Z `now`, and False when `context.now is None`. `not_in_time_window` is the exact boolean inverse for the Tuesday/Saturday cases and is **True** when `context.now is None` (fail-closed for an outside-the-window gate).
14. `evaluate_condition` raises `ValueError` for an `IN` op whose `value` is not a list and for a `field` outside the supplied whitelist.
15. **Totality:** a Hypothesis property test over random `ToolCall` + random `PolicyContext` + a random valid `Policy` always returns a `Decision` and never raises (extends F04's totality guarantee to the conditional layer).
16. `POST /policy/repos/{id}/simulate` returns 200 with `decision`, `base_effect`, and a `traces` entry per rule (matched flag correct); persists **no** `PolicyRuleEvaluation` row. Unauthenticated → 401; `viewer` → 403.
17. `evaluate_tool_call(..., context, agent_run_id, step_id)` persists exactly one append-only `PolicyRuleEvaluation` row **iff** ≥1 conditional rule matched; the row's `final_effect`, `base_effect`, `matched_rule_ids`, and `context_redacted` match the decision; `context_redacted` contains no `command` value.
18. `GET /policy/repos/{id}/rule-evaluations?agent_run_id=...` returns the rows for that run, newest first, workspace-scoped (cross-workspace id → empty/404).
19. `forge policy test examples/policies/conditional/deploy-time-gated.yaml` exits 0 when its `.tests.yaml` passes and exits 1 (printing the failing case name, expected, actual) when an assertion is altered.
20. All `examples/policies/conditional/*.yaml` load with `is_valid=True` and each ships a `*.tests.yaml` that runs green; the regenerated `policy.schema.json` matches `Policy.model_json_schema()` (v2 drift guard).
21. **Merge gate is immutable (human approval before merge — always):** against a policy with `review_rules.approval_required_for_merge: true`, an `override_base: true` allow rule that matches `ToolCall(merge, args={branch:"main"})` (base → `deny`, `requires_approval=True`) → still `deny`, `requires_approval=True`, `base_effect="deny"`, with the allow recorded in `conditional_matches` but `effect` unchanged; a conditional `deny`/`require_approval` rule on `merge`/`push` is still honored (tightening allowed). `push` to the base branch behaves identically.

### Traceability — requirement → criteria

| Spec / F04 requirement | Criteria |
|---|---|
| Conditional rules extend the flat policy without breaking it | 1, 2, 5, 12 |
| Rule schema validation (fail-closed, no silent widening) | 2, 3, 14 |
| Conditional tighten (deny / require-approval) | 4, 6, 10, 11 |
| Bounded, explicit loosening only | 7, 8, 9, 21 |
| Critical floor immutable (traversal, secrets, unknown action) | 8, 9 |
| Human approval before PR merge — always (never loosened) | 21 |
| Deterministic, total evaluation (no LLM) | 11, 12, 13, 15 |
| Auditability of conditional decisions (rich row + central audit log) | 16, 17, 18 |
| Policy-as-code testing / eval-first | 19, 20 |
| RBAC on new routes | 16, 18 |
| OSS example policies + schema drift guard | 19, 20 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to a criterion. Layout extends F04's test tree.

**Unit — conditions (`packages/contracts/tests/test_conditions.py`):**
- `test_empty_group_true`, `test_all_vs_any`, `test_nested_groups` (AC12).
- `test_ops_table` — parametrized over every `ConditionOp` with matching/non-matching values (AC13/14): EQ/NE/IN/NOT_IN/LT.../CONTAINS/IS_NULL/IS_NOT_NULL/MATCHES_GLOB/IN_TIME_WINDOW/NOT_IN_TIME_WINDOW.
- `test_field_not_in_whitelist_raises` / `test_in_op_requires_list` (AC14).
- `test_missing_field_is_none_semantics` — positive ops False, IS_NULL True, NOT_IN_TIME_WINDOW True (fail-closed).
- `test_time_window_weekday_and_hour` (AC13) — `in_time_window` Tuesday True / Saturday False / `now is None` False; `not_in_time_window` is the inverse and is True when `now is None`.

**Unit — schema (`packages/policy-sdk/tests/test_conditional_schema.py`):**
- `test_rules_require_v2` (AC2); `test_duplicate_rule_id_rejected`, `test_unknown_condition_field_rejected`, `test_unknown_applies_to_rejected` (AC3).
- `test_decision_additive_fields_default_empty` (AC1 shape).
- `test_json_schema_v2_matches_committed` (AC20).

**Unit — evaluator (`packages/policy-sdk/tests/test_conditional_evaluator.py`):** table-driven over `(policy, tool_call, context) → (expect_effect, expect_requires_approval, expect_matched_rule, expect_base_effect)`:
- `test_v1_policy_matches_f04_base` — re-run the **entire F04 `tool_call_matrix`** through `ConditionalPolicyEvaluator` and assert equality with `DefaultPolicyEvaluator` (AC1, the regression lock).
- `test_conditional_deny_tightens_allow` (AC4); `test_condition_false_passes_through` (AC5).
- `test_gate_escalates_base_allow` (AC6).
- `test_override_loosens_noncritical_deny` + `test_override_denied_for_non_admin` (AC7).
- `test_override_cannot_defeat_secret_floor` (AC8); `test_override_cannot_defeat_traversal` (AC9).
- `test_override_cannot_defeat_merge_gate` + `test_conditional_can_still_tighten_merge` (AC21).
- `test_deny_beats_override` (AC10); `test_priority_ordering_deterministic` (AC11).
- `test_gate_does_not_fire_in_window_passes_through` (AC6 in-window pass-through).
- `test_evaluate_is_total` — Hypothesis over random valid `Policy` + `ToolCall` + `PolicyContext` (AC15).

**Unit — context (`packages/policy-sdk/tests/test_context.py`):**
- `test_to_fields_derives_weekday_hour_ext` ; `test_to_redacted_fields_drops_command` (AC17).
- `test_build_context_from_run` shape.

**Unit — policy tests runner (`packages/policy-sdk/tests/test_tests_runner.py`):**
- `test_suite_passes` / `test_suite_reports_failure` (AC19) — a suite with one altered expectation → `ok is False`, failure carries name/expected/actual.

**Unit — examples (`packages/policy-sdk/tests/test_examples_conditional.py`, AC20):** parametrize over `examples/policies/conditional/*.yaml` → `load_policy` `is_valid` and `run_policy_tests` green.

**Integration — API (`apps/api/tests/test_policy_conditional_routes.py`, ASGI httpx + Postgres test-container):**
- `test_simulate_ok_no_persistence` (AC16) — seed a `RepositoryConnection`, stub integration-sdk `read_file` to the canonical v2 YAML; assert `SimulationResult` shape and that no `policy_rule_evaluation` row was written.
- `test_simulate_unauth_401` / `test_simulate_viewer_403` (AC16).
- `test_evaluate_persists_rule_evaluation` (AC17) — call `evaluate_tool_call` with `agent_run_id`/`step_id` against a policy where a rule matches; assert one row, fields, redacted context, append-only (second identical call → second row, no update), **and** that exactly one `policy.decision` `AuditEvent` was emitted to the injected in-memory `AuditSink` with no `command`/raw-args in the payload.
- `test_immutability_trigger_blocks_update` — a raw `UPDATE`/`DELETE` against `policy_rule_evaluation` is rejected at the DB level (F39 `attach_immutability_trigger`).
- `test_rule_evaluations_query_scoped` (AC18).
- `test_test_endpoint_runs_suite` — `POST /policy/repos/{id}/test` stubbed to a `.tests.yaml`.

**CLI (`apps/api/tests/test_policy_conditional_cli.py`, AC19):** `forge policy simulate` prints the decision + matched rules; `forge policy test` exit 0/1 via `CliRunner`.

**Frontend (`apps/web`, Vitest + RTL):**
- `ConditionalRulesView` renders rule cards + effect/severity badges from a mocked `EffectivePolicyResponse`.
- `RuleSimulator` posts the form and renders the matched-rule trace + base-vs-final effect.
- Approval panel renders `conditional_matches` under "Risks flagged".

**Key fixtures:**
- `packages/policy-sdk/tests/fixtures/policy_conditional_canonical.yaml` — the §4 canonical v2 policy (golden input).
- `.../fixtures/policy_conditional_canonical.tests.yaml` — its assertion suite.
- `.../fixtures/policy_v1_flat.yaml` — a `schema_version: 1` policy for the regression lock (reuse F04's `policy_canonical.yaml`).
- `conditional_decision_matrix` — the parametrized `(policy, tool_call, context) → decision` table, shared with the golden eval harness (F12).
- `recorded/github_read_file_policy_v2.json` — recorded integration-sdk response (no live GitHub).

---

## 8. Security & policy considerations

- **The agent never self-expands scope (Build Prompt #2).** Loosening (`effect: allow`) requires an explicit `override_base: true` authored by a human in the repo-resident policy file; the agent cannot author or alter rules at runtime, and `override` can only flip a **non-critical, non-merge-gate** base deny. When a rule escalates to `require_approval`, the call is paused and routed to F36's admin-only `policy_override` gate, which mints a single-use, short-TTL `PolicyOverrideGrant` bound to the exact action fingerprint — the grant never broadens future scope and the agent can never approve it.
- **Immutable floor.** Base denials with `severity == "critical"` (path traversal, absolute paths, secret-file globs `*.pem`/`*.key`/`.env*`/`secrets/**`, disabled/restricted-environment deploy, unknown action) **and the PR-merge approval gate** (`merge`/`push`, where "human approval is required before PR merge — always", Build Prompt #5) are returned unchanged regardless of any matching conditional allow — encoded as the first branch of the ladder and asserted by AC8/AC9/AC21. This guarantees conditional rules cannot widen permissions past F04's hard guarantees nor relax the mandatory merge gate.
- **Deny precedence preserved and extended.** Conditional `deny` beats conditional `allow` (AC10); F04's intra-`write_rules` deny precedence is untouched because the conditional layer runs *after* and *on top of* the base decision.
- **Fail-closed validation.** `extra="forbid"` on `ConditionalRule`; unknown condition fields, unknown actions, duplicate ids, and `rules`-without-v2 are hard validation errors — a typo cannot silently disable a deny rule or smuggle an unrecognized field.
- **Deterministic, no LLM, total function.** Evaluation is pure boolean predicate matching with a fixed precedence ladder (mirrors the "Supervisor makes routing decisions via explicit policy, not LLM judgement" principle); the Hypothesis totality test (AC15) guards against an unhandled action/context crashing the gate (which would otherwise fail open).
- **Audit & redaction.** Every conditional decision that fired a rule writes an append-only `policy_rule_evaluation` row (DB-level immutability via F39's `attach_immutability_trigger`) **and** emits a compact `policy.decision` `AuditEvent` into the platform's central immutable, hash-chained `audit_log` (`cross-cutting/F39-audit-log`), satisfying Build Prompt #9 / Security §"Audit log". `context_redacted` drops `command` (may embed tokens) and truncates `path` via the canonical `SecretRedactor` (`cross-cutting/F37-auth-secrets-byok`), and F39's `SqlAuditWriter` redacts again before persistence — log `effect`/`severity`/`matched_rule_ids` only, never raw `ToolCall.args`, mirroring F04/F10 trace redaction.
- **Time-conditional integrity.** The evaluation clock is part of the (runtime-supplied) `PolicyContext.now`, not read inside the pure evaluator — so decisions are reproducible/replayable (F12 replay) and tests are hermetic. The context-builder contract requires the runtime to set `now` (UTC); the linter warns when a rule uses time fields so maintainers know a clock is required.
- **Tenant isolation.** `PolicyRuleEvaluation` and all new queries are workspace-scoped (AC18); simulate/test routes resolve the repo within the caller's workspace.
- **AGENTS.md / narrative remains non-authoritative.** As in F04, conditional rules gate actions regardless of what `AGENTS.md` instructs — a malicious narrative cannot introduce or disable a rule.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Overall: L.** The `conditions` primitive + `ConditionalPolicyEvaluator` ladder + `PolicyContext` are **M** and the critical path (security-sensitive composition); the audit table, simulate/test routes, CLI, UI simulator, and examples add the rest. Smaller than a from-scratch feature because it reuses F04's loader, matching, snapshot, router, and web route wholesale.

| Risk | Severity | Mitigation |
|---|---|---|
| Composition semantics accidentally **widen** permissions (the headline risk) | High | The precedence ladder is fully specified in §4 with an immutable floor over critical denials **and the merge gate**; AC7/8/9/10/21 lock loosening to explicit non-critical, non-merge-gate overrides; the v1-equivalence regression test (AC1) + Hypothesis totality (AC15) guard the boundaries |
| Conditional layer diverges from F04 on `schema_version: 1` inputs | High | AC1 re-runs F04's **entire** `tool_call_matrix` through the new evaluator and asserts byte-equality; CI gate |
| Time/clock non-determinism breaks replay/tests | Medium | `now` lives in `PolicyContext`, injected by the runtime; the evaluator reads no wall clock; hermetic time-window tests (AC13) |
| Condition field/op surface grows unbounded / inconsistent with F21 | Medium | One shared `forge_contracts.conditions` primitive + a fixed `POLICY_CONDITION_FIELDS` whitelist; new fields require a schema bump + linter update |
| Rule authoring footguns (self-contradicting rules, override on secret paths) | Medium | Linter warnings (`override_on_secret_path`, `time_rule_requires_clock`, `unreachable_rule`); `.forge/policy.tests.yaml` + `forge policy test` make policies TDD-gated in the repo's CI |
| Audit-row volume on hot tool loops | Low | Rows written only when ≥1 conditional rule matched; indexed on `agent_run_id`; flat-only repos write none |

---

## 10. Key files / paths (exact)

**Shared contracts:**
- `packages/contracts/forge_contracts/conditions.py` (NEW — `ConditionOp`/`Condition`/`ConditionGroup`/`evaluate_condition`)
- `packages/contracts/forge_contracts/policy.py` (extend `Policy` with `rules`/`schema_version`; add `RuleEffect`/`ConditionalRule`/`ConditionalMatch`; extend `Decision`)
- `packages/contracts/tests/test_conditions.py`

**Core package (extends F04 `policy-sdk`):**
- `packages/policy-sdk/forge_policy/schema.py` (re-export the new models)
- `packages/policy-sdk/forge_policy/context.py` (NEW)
- `packages/policy-sdk/forge_policy/conditional.py` (NEW — `ConditionalPolicyEvaluator`)
- `packages/policy-sdk/forge_policy/tests_runner.py` (NEW)
- `packages/policy-sdk/forge_policy/errors.py` (add `PolicyRuleError`)
- `packages/policy-sdk/forge_policy/policy.schema.json` (regenerated for v2)
- `packages/policy-sdk/tests/{test_conditional_schema,test_conditional_evaluator,test_context,test_tests_runner,test_examples_conditional}.py`
- `packages/policy-sdk/tests/fixtures/{policy_conditional_canonical.yaml,policy_conditional_canonical.tests.yaml,policy_v1_flat.yaml}`

**Data model + migration:**
- `packages/db/forge_db/models/policy_rule_evaluation.py` (NEW)
- `packages/db/migrations/versions/xxxx_f29_policy_rule_evaluation.py`

**API:**
- `apps/api/forge_api/routers/policy.py` (add simulate/test/rule-evaluations; extend evaluate with context)
- `apps/api/forge_api/services/policy_service.py` (extend evaluate/simulate/run_policy_tests)
- `apps/api/forge_api/schemas/policy.py` (add `SimulateRequest`/`SimulationResult`/`RuleTrace`/`PolicyRuleEvaluationOut`)
- `apps/api/forge_api/cli/policy.py` (add `simulate`, `test`)
- `apps/api/tests/{test_policy_conditional_routes,test_policy_conditional_cli}.py`

**Frontend:**
- `apps/web/app/(board)/projects/[projectId]/repos/[repoId]/policy/page.tsx` (extend)
- `apps/web/components/policy/{ConditionalRulesView,RuleSimulator,PolicyTestPanel}.tsx`
- `apps/web/lib/hooks/usePolicy.ts` (add `useSimulatePolicy`, `usePolicyTests`, `useRuleEvaluations`)

**Examples:**
- `examples/policies/conditional/{deploy-time-gated,infra-branch-gated,run-command-env-role,subagent-mode-gated}.yaml` (+ matching `*.tests.yaml`)

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md §"Phased Roadmap → Phase 3 (V3)" — *"Advanced policy engine with conditional rules"* (the roadmap line this slice implements).
- FORGE_SPEC.md §"Repo Policy System" — the flat `policy.yaml` schema F29 refines (the `rules:` block is additive on top of it).
- FORGE_SPEC.md §"Security" — *"Policy evaluation: Every tool invocation checked against repo policy before execution"*; secret redaction; tenant isolation — the invariants the conditional layer must preserve.
- FORGE_SPEC.md §"Build Prompt" constraints #2 (*"The agent never self-assigns permissions or expands its own scope"*) and #9 (*"An audit log exists for every agent action, tool call, and MCP call"*).
- FORGE_SPEC.md §"Human Approval System" — Approval Gate *"Policy override — Always required"* and *"PR approval — Always required before merge"* (the `policy_override` gate F29's `require_approval` raises, and the merge gate F29 can never loosen).
- FORGE_SPEC.md §"Build Prompt" constraint #5 (*"Human approval is required before PR merge — always"*) — the basis for the merge-gate floor (AC21).
- FORGE_SPEC.md §"Workflow Engine" — `escalation_policy.on_policy_conflict: escalate_to_admin` and the DSL's `when:`/`condition:` constructs (the conditional vocabulary this generalizes for policy).
- FORGE_SPEC.md §"Multi-Agent Orchestration" — *"Supervisor makes routing decisions via explicit policy, not LLM judgement"* — the deterministic-not-LLM principle applied to the policy engine.
- FORGE_SPEC.md §"Observability and Evaluation" — *"Evaluation is built in from day one"* — motivates `.forge/policy.tests.yaml` policy-as-code testing.
- `docs/implementation-slices/v1/F04-repo-policy.md` — the base evaluator/loader/snapshot/router F29 extends; §12 of F04 explicitly defers this conditional engine to V3.
- `docs/implementation-slices/cross-cutting/F36-human-approval-system.md` — owns the `policy_override` gate primitive, the `GateContextProvider` registry, the single-use `PolicyOverrideGrant`, and the Approval UI "Risks flagged" panel F29 plugs into (F36 §"Downstream consumers" names F29 explicitly).
- `docs/implementation-slices/cross-cutting/F39-audit-log.md` — the canonical `AuditEvent`/`AuditSink`/`SqlAuditWriter` and `attach_immutability_trigger(table)` helper F29 uses for the `policy.decision` audit emission and table immutability.
- `docs/implementation-slices/cross-cutting/F37-auth-secrets-byok.md` — the `Principal`/`require_role` RBAC dependency gating the new routes and the canonical `SecretRedactor` used for `context_redacted`.
- `docs/implementation-slices/v2/F21-workflow-automations.md` — the deterministic, policy-bounded condition DSL (`Condition`/`ConditionGroup`/`ConditionOp`) F29 lifts into shared contracts.
- `pathspec` (gitwildmatch) — reused from F04 for the `matches_glob` op (path/branch globs).

---

## 12. Out of scope / future

- **Migrating F21's automation engine onto `forge_contracts.conditions`** — F29 introduces the shared primitive and uses it; refactoring `forge_automation` to consume it (and deleting its duplicate) is a follow-up that must not regress F21's tests.
- **Deployment gates & environment-promotion matrices (`v3/F31-deployment-gates`)** — F29 provides the conditional `deploy`/`promote_environment` rule primitive; the promotion-workflow states, environment registry, and approval routing live in F31.
- **Task-level `allowed_actions`/`restricted_actions` composition** — still owned by the runtime tool gate (F04 §12 / F06); F29 composes repo flat-policy + conditional rules only, then the runtime overlays task-level lists.
- **Cross-repo / workspace-level conditional policies** — F29 is per-repo (`.forge/policy.yaml`). Workspace-wide policy inheritance and multi-team RBAC-conditional rules ride on `v3/F30-multi-team-rbac`.
- **Rule expression language beyond declarative groups** (arbitrary boolean expressions, arithmetic, regex on free fields) — intentionally excluded to keep evaluation total, whitelisted, and non-Turing-complete; the `ConditionGroup` all/any tree + fixed op set is the ceiling.
- **`max_parallel` subagent concurrency enforcement** — still the multi-agent coordinator's job (F04 §12); conditional rules can gate `spawn_subagent` by role/mode but not count instances.
- **In-UI authoring/commit of conditional rules back to the repo** — the UI stays read + simulate + test; rules are edited in git (consistent with F04's read-only policy UI).
- **MCP-specific conditional rules** (namespace/freshness-conditional MCP tool gating) — MCP calls reuse `evaluate_in_context` via the generic `ToolCall` path; MCP-specific condition fields live in the MCP SDK slice referencing F29.
