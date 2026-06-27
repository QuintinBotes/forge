"""Human-readable markdown renderers for spec artifacts.

These produce the narrative companions to ``manifest.yaml`` (FORGE_SPEC: Spec
Folder Layout): ``spec.md``, ``clarify.md``, ``plan.md``, ``tasks.md``,
``validation.md``, ``decisions.md`` and the project ``constitution.md``. Each is
a pure function of its inputs so artifacts are reproducible.
"""

from __future__ import annotations

from forge_contracts import (
    Constitution,
    RequirementTrace,
    SpecManifest,
    TaskDTO,
    ValidationReport,
)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "_None_"


def render_constitution_md(constitution: Constitution) -> str:
    lines = ["# Constitution", ""]
    if constitution.project_id is not None:
        lines += [f"Project: `{constitution.project_id}`", ""]
    lines += ["## Engineering Principles", "", _bullets(constitution.principles), ""]
    lines += [
        "## Architecture Guardrails",
        "",
        _bullets(constitution.architecture_guardrails),
        "",
    ]
    return "\n".join(lines) + "\n"


def render_spec_md(manifest: SpecManifest) -> str:
    lines = [f"# {manifest.id} — {manifest.name}", "", f"Status: **{manifest.status.value}**", ""]
    lines += ["## Requirements", ""]
    lines += [f"- **{r.id}**: {r.text}" for r in manifest.requirements] or ["_None_"]
    lines += ["", "## Acceptance Criteria", ""]
    lines += [
        f"- **{a.id}** (refs: {', '.join(a.req_refs) or '—'}): {a.text}"
        for a in manifest.acceptance_criteria
    ] or ["_None_"]
    lines += ["", "## Constraints", "", _bullets(manifest.constraints), ""]
    return "\n".join(lines) + "\n"


def render_clarify_md(manifest: SpecManifest) -> str:
    lines = ["# Clarifications", ""]
    if not manifest.open_questions:
        lines += ["_No open questions._", ""]
    for q in manifest.open_questions:
        resolution = f"**Resolution:** {q.resolution}" if q.resolution else "**Resolution:** _open_"
        lines += [f"## {q.id}", "", f"**Q:** {q.text}", "", resolution, ""]
    return "\n".join(lines) + "\n"


def render_plan_md(manifest: SpecManifest) -> str:
    lines = [f"# Plan — {manifest.id}", "", "## Approach", ""]
    lines += [
        "Derived from the approved spec. Each requirement maps to one or more "
        "implementation tasks with explicit acceptance criteria.",
        "",
        "## Architecture Decisions",
        "",
    ]
    if manifest.decisions:
        lines += [f"- **{d.id}** ({d.status}): {d.title}" for d in manifest.decisions]
    else:
        lines += ["_None recorded._"]
    lines += ["", "## Repositories", "", _bullets(manifest.repos), ""]
    return "\n".join(lines) + "\n"


def render_decisions_md(manifest: SpecManifest) -> str:
    lines = ["# Architecture Decision Records", ""]
    if not manifest.decisions:
        lines += ["_No decisions recorded._", ""]
    for d in manifest.decisions:
        lines += [f"## {d.id} — {d.title}", "", f"Status: **{d.status}**", ""]
        if d.context:
            lines += ["### Context", "", d.context, ""]
        if d.decision:
            lines += ["### Decision", "", d.decision, ""]
        if d.consequences:
            lines += ["### Consequences", "", d.consequences, ""]
    return "\n".join(lines) + "\n"


def render_tasks_md(manifest: SpecManifest, tasks: list[TaskDTO]) -> str:
    lines = [f"# Tasks — {manifest.id}", ""]
    if not tasks:
        lines += ["_No tasks generated._", ""]
    for task in tasks:
        criteria = ", ".join(a.id for a in task.acceptance_criteria) or "—"
        lines += [f"## {task.key} — {task.title}", "", f"Acceptance: {criteria}", ""]
    return "\n".join(lines) + "\n"


def render_validation_md(report: ValidationReport) -> str:
    lines = [
        f"# Validation — {report.task_id}",
        "",
        f"Spec: `{report.spec_id}`",
        f"Result: **{'PASS' if report.passed else 'FAIL'}**",
        "",
        "## Requirement Traceability",
        "",
        "| Requirement | Acceptance | Tasks | Tests | Satisfied |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [_trace_row(row) for row in report.traceability]
    if report.checks:
        lines += ["", "## Verification Checks", ""]
        lines += ["| Check | Result | Details |", "| --- | --- | --- |"]
        for c in report.checks:
            lines.append(f"| {c.name} | {'pass' if c.passed else 'fail'} | {c.details or ''} |")
    if report.coverage is not None:
        lines += ["", f"Coverage: **{report.coverage}%**"]
    if report.notes:
        lines += ["", "## Notes", "", *[f"- {note}" for note in report.notes]]
    return "\n".join(lines) + "\n"


def _trace_row(row: RequirementTrace) -> str:
    acceptance = ", ".join(row.acceptance_criteria_ids) or "—"
    tasks = ", ".join(row.task_refs) or "—"
    tests = ", ".join(row.test_refs) or "—"
    return f"| {row.requirement_id} | {acceptance} | {tasks} | {tests} | {row.satisfied} |"


__all__ = [
    "render_clarify_md",
    "render_constitution_md",
    "render_decisions_md",
    "render_plan_md",
    "render_spec_md",
    "render_tasks_md",
    "render_validation_md",
]
