"""Render a list of :class:`GateResult` into ``RELEASE_READINESS.md``.

The verbatim honest-asterisk footer is the policy guarantee that ships in every
report (and, via the release workflow, in the GitHub Release notes): a green gate
can never *imply* a human pentest, a real fleet soak, or SOC2/compliance.
"""

from __future__ import annotations

from collections.abc import Sequence

from forge_eval.release.model import Bar, GateResult, bar_met

# Reproduced verbatim from SPEC-PRODUCTION-HARDENING.md (DoD item 22). Any drift
# from the spec is caught by the render golden test.
HONEST_ASTERISK = (
    "Code- and evidence-ready for production, pending an external human "
    "penetration test and a real multi-week multi-tenant fleet soak — neither "
    "performable by the build agents; both are named, scoped, and handed off."
)
COMPLIANCE_NOTE = "Compliance attestation (SOC2 etc.) is out of scope."

_COLUMNS = ["Gate", "Blocker", "Workstream", "Status", "Evidence (cmd/artifact)", "Last-checked"]


def _cell(value: str) -> str:
    """Make a value safe to place inside a Markdown table cell."""

    return value.replace("|", "\\|").replace("\n", " ").strip()


def _row(result: GateResult) -> str:
    gate = result.gate
    blocker = f"#{gate.blocker}" if gate.blocker is not None else "—"
    cells = [
        f"`{gate.id}`",
        blocker,
        _cell(gate.workstream or "—"),
        f"{result.status.symbol} {result.status.value}",
        _cell(gate.evidence_ref or "—"),
        _cell(result.checked_at or "—"),
    ]
    return "| " + " | ".join(cells) + " |"


def _table(results: Sequence[GateResult]) -> list[str]:
    lines = ["| " + " | ".join(_COLUMNS) + " |", "|" + "|".join(["---"] * len(_COLUMNS)) + "|"]
    lines.extend(_row(r) for r in results)
    return lines


def render_markdown(
    results: Sequence[GateResult],
    *,
    target: Bar,
    git_sha: str = "unknown",
    cz_version: str = "unknown",
    generated_at: str = "",
) -> str:
    """Render the full ``RELEASE_READINESS.md`` document as a string."""

    met = bar_met(results, target)
    verdict = "✅ **MET**" if met else "❌ **NOT MET**"

    lines: list[str] = [
        "# Release Readiness",
        "",
        f"- **Target bar:** {target.label}",
        f"- **Overall verdict:** {verdict}",
        f"- **Generated (UTC):** {generated_at or '—'}",
        f"- **Commit:** `{git_sha}`",
        f"- **Version (cz):** `{cz_version}`",
        "",
        (
            "> A bar is **MET** only when every gate at-or-below it is `GREEN` or "
            "`MANUAL_ATTESTED`. `SKIPPED_NO_CREDS`, `MISSING_EVIDENCE`, `STALE`, "
            "`MANUAL_PENDING`, and `RED` all mean **NOT MET** — the engine never "
            "infers a pass."
        ),
        "",
    ]

    for bar in Bar:
        tier = [r for r in results if r.gate.bar == bar]
        if not tier:
            continue
        selected = "included" if bar <= target else "not evaluated for this bar"
        lines.append(f"## {bar.label} gates ({selected})")
        lines.append("")
        lines.extend(_table(tier))
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    selected_results = [r for r in results if r.gate.bar <= target]
    not_met = [r for r in selected_results if not r.met]
    if met:
        lines.append(
            f"The **{target.label}** bar is **MET**: all {len(selected_results)} "
            "selected gates are GREEN or MANUAL_ATTESTED."
        )
    else:
        lines.append(
            f"The **{target.label}** bar is **NOT MET**: "
            f"{len(not_met)}/{len(selected_results)} selected gates are not satisfied."
        )
        lines.append("")
        for r in not_met:
            lines.append(f"- `{r.gate.id}` — {r.status.value}: {_cell(r.detail)}")
    lines.append("")

    lines.append("## Honest asterisk (verbatim)")
    lines.append("")
    lines.append(f"> {HONEST_ASTERISK}")
    lines.append(">")
    lines.append(f"> {COMPLIANCE_NOTE}")
    lines.append("")

    return "\n".join(lines) + "\n"
