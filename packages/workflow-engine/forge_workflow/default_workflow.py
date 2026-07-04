"""The built-in ``default_feature`` workflow (spec: Workflow Engine).

Mirrors the spec's "Default Feature Workflow States" and "Workflow DSL"
sections verbatim, filling in the intermediate ``action`` transitions the spec
elides so the full ``created -> ... -> closed`` chain is walkable. Every state
is a :class:`forge_contracts.WorkflowState` enum member so engine transitions
are typed.
"""

from __future__ import annotations

from functools import lru_cache

from forge_contracts import WorkflowDefinition

#: Canonical name of the shipped default workflow.
DEFAULT_WORKFLOW_NAME = "default_feature"

#: The default feature workflow as DSL (the spec's example, completed).
DEFAULT_FEATURE_WORKFLOW_YAML = """\
workflow: default_feature
version: "1"
modes:
  default: single_agent
  optional: [supervised_multi_agent]

transitions:
  - from: created
    to: spec_drafting
    action: generate_spec_draft
    skill: spec-analyst

  - from: spec_drafting
    to: clarification
    action: gather_clarifications

  - from: clarification
    to: spec_review
    action: submit_spec_for_review

  - from: spec_review
    to: spec_approved
    when: spec_approved_by_human
    record: approval_event

  - from: spec_review
    to: clarification
    when: spec_changes_requested

  - from: spec_approved
    to: plan_drafting
    action: generate_plan

  - from: plan_drafting
    to: plan_review
    action: submit_plan_for_review

  - from: plan_review
    to: task_generation
    when: plan_approved_by_human
    record: approval_event

  - from: task_generation
    to: task_ready
    action: generate_tasks

  - from: task_ready
    to: executing
    action: start_agent_run
    preconditions: [repo_target_set, policy_loaded, skill_profile_set, knowledge_synced]

  - from: executing
    to: verifying
    action: run_checks
    checks: [lint, type_check, tests, coverage]

  - from: executing
    to: needs_human_input
    when: low_confidence

  - from: verifying
    to: pr_opened
    when: all_checks_passed
    action: open_pr_with_spec_traceability

  - from: verifying
    to: executing
    when: checks_failed
    condition: retry_budget_remaining

  - from: verifying
    to: needs_human_input
    when: checks_failed
    condition: retry_budget_exhausted

  - from: pr_opened
    to: awaiting_review
    action: request_reviews

  - from: awaiting_review
    to: merged
    when: [review_approved_by_human, ci_status_green, spec_validated]

  - from: merged
    to: closed
    action: close_task

retry_policy:
  max_retries: 3
  backoff: exponential
  initial_delay_seconds: 30

escalation_policy:
  confidence_threshold: 0.72
  on_low_confidence: pause_and_notify
  on_policy_conflict: escalate_to_admin
"""


@lru_cache(maxsize=1)
def default_feature_definition() -> WorkflowDefinition:
    """Return the parsed, validated default feature :class:`WorkflowDefinition`."""
    # Imported here to avoid a module-level import cycle (dsl -> fsm -> ...).
    from forge_workflow.dsl import parse_definition

    return parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)


__all__ = [
    "DEFAULT_FEATURE_WORKFLOW_YAML",
    "DEFAULT_WORKFLOW_NAME",
    "default_feature_definition",
]
