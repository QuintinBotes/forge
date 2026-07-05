"""Postmortem composition (F17) — deterministic V1, no LLM.

``TemplatePostmortemComposer`` assembles a structured :class:`Postmortem` from the
real incident timeline (state changes, context findings, impact, runbook steps,
recovery checks) and renders deterministic markdown. The LLM-backed composer
plugs in behind the same :class:`PostmortemComposer` Protocol later (same pattern
as the spec engine's template generators). Action items are derived from the
structured timeline, not free-form parsing.
"""

from __future__ import annotations

import hashlib

from forge_contracts.incident import (
    ActionItem,
    IncidentEventDTO,
    IncidentSnapshot,
    Postmortem,
    PostmortemTimelineEntry,
    Runbook,
)

__all__ = ["TemplatePostmortemComposer", "render_postmortem_md"]


class TemplatePostmortemComposer:
    """A deterministic :class:`PostmortemComposer` over the incident timeline."""

    def compose(
        self,
        *,
        incident: IncidentSnapshot,
        events: list[IncidentEventDTO],
        plans: list[Runbook],
    ) -> Postmortem:
        ordered = sorted(events, key=lambda e: (e.sequence, e.created_at or _epoch()))
        timeline = [
            PostmortemTimelineEntry(at=ev.created_at or _epoch(), summary=ev.summary)
            for ev in ordered
            if ev.created_at is not None or ev.summary
        ]

        findings = [ev.summary for ev in ordered if ev.kind == "context_finding"]
        impact_events = [ev for ev in ordered if ev.kind == "impact"]
        root_cause = (
            impact_events[-1].summary
            if impact_events
            else (findings[0] if findings else "Root cause not determined.")
        )
        affected = _affected_services(incident, impact_events)

        resolution = _resolution(ordered)
        action_items = _action_items(incident, plans, affected, root_cause)

        return Postmortem(
            incident_id=incident.id,
            summary=f"{incident.title} (severity: {incident.severity.value})",
            timeline=timeline,
            root_cause=root_cause,
            contributing_factors=findings,
            resolution=resolution,
            lessons_learned=[
                "Detection-to-acknowledgement and remediation steps are captured "
                "in the immutable incident timeline.",
                "Every mutating remediation required an explicit human approval.",
            ],
            action_items=action_items,
        )


def _epoch():
    from datetime import UTC, datetime

    return datetime(1970, 1, 1, tzinfo=UTC)


def _affected_services(
    incident: IncidentSnapshot, impact_events: list[IncidentEventDTO]
) -> list[str]:
    services: list[str] = []
    for ev in impact_events:
        for svc in ev.data.get("affected_services", []) or []:
            if svc not in services:
                services.append(str(svc))
    return services


def _resolution(events: list[IncidentEventDTO]) -> str:
    steps = [ev.summary for ev in events if ev.kind == "runbook_step"]
    if steps:
        return "Remediation steps applied: " + "; ".join(steps)
    return "Incident resolved after recovery signals returned to healthy."


def _action_items(
    incident: IncidentSnapshot,
    plans: list[Runbook],
    affected: list[str],
    root_cause: str,
) -> list[ActionItem]:
    items: list[ActionItem] = [
        ActionItem(
            title=f"Postmortem follow-up: {incident.title}",
            description=(
                f"Address the root cause of incident {incident.key or incident.id}: {root_cause}"
            ),
            kind="bug",
            priority="high",
        )
    ]
    target = affected[0] if affected else (incident.title or "the affected service")
    items.append(
        ActionItem(
            title=f"Add/verify monitoring and alerting for {target}",
            description=(
                "Ensure detection coverage so a recurrence is caught earlier "
                "(derived from the incident impact assessment)."
            ),
            kind="chore",
            priority="medium",
        )
    )
    for plan in plans:
        for step in plan.steps:
            if step.blast_radius.value != "low":
                items.append(
                    ActionItem(
                        title=f"Make remediation durable: {step.title}",
                        description=(
                            f"The remediation step {step.id!r} ({step.action}) was a "
                            "manual mitigation; convert it into a permanent fix."
                        ),
                        kind="chore",
                        priority="medium",
                    )
                )
    return items


def render_postmortem_md(postmortem: Postmortem) -> str:
    """Render a :class:`Postmortem` to deterministic markdown."""
    lines: list[str] = [f"# Postmortem: {postmortem.summary}", ""]
    lines += ["## Root Cause", "", postmortem.root_cause or "_unknown_", ""]
    lines += ["## Resolution", "", postmortem.resolution or "_n/a_", ""]

    lines += ["## Timeline", ""]
    if postmortem.timeline:
        for entry in postmortem.timeline:
            lines.append(f"- `{entry.at.isoformat()}` — {entry.summary}")
    else:
        lines.append("_No timeline entries recorded._")
    lines.append("")

    lines += ["## Contributing Factors", ""]
    if postmortem.contributing_factors:
        lines += [f"- {factor}" for factor in postmortem.contributing_factors]
    else:
        lines.append("_None recorded._")
    lines.append("")

    lines += ["## Lessons Learned", ""]
    lines += [f"- {lesson}" for lesson in postmortem.lessons_learned]
    lines.append("")

    lines += ["## Action Items", ""]
    for item in postmortem.action_items:
        owner = f" (owner: {item.owner_hint})" if item.owner_hint else ""
        lines.append(f"- [{item.kind}/{item.priority}] {item.title}{owner} — {item.description}")
    lines.append("")
    return "\n".join(lines)


def content_hash(content_md: str) -> str:
    """Return the sha256 hex digest of rendered postmortem markdown."""
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()
