"""``summary.json`` and the markdown report of a graded run (spec J §2.4.1).

Both hold ids, verdicts and numbers only, never answer text or judge
reasons (those stay in the private run store), so they can be published as
CI artifacts and committed. Reference rows are recomputed from
``reference.json`` by the same scoring code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from forge_eval.ctxbench.bank import CATEGORIES, Bank, Category
from forge_eval.ctxbench.judge import Verdict
from forge_eval.ctxbench.score import GradedAsk, reference_asks, summarize

__all__ = ["REFERENCE_ARMS", "build_summary", "render_markdown"]

#: Published arms recomputed on these 30 questions, with their report labels.
REFERENCE_ARMS = {"lore": "Lore (published)", "utopia": "Utopia (published)"}
_LETTER = {"correct": "C", "partial": "P", "wrong": "W", "dont_know": "A"}


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, Mapping):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    return value


def _ask(row: Mapping[str, Any]) -> GradedAsk:
    return GradedAsk(
        question_id=row["question_id"],
        category=cast(Category, row["category"]),
        rep=int(row["rep"]),
        verdict=cast(Verdict, row["verdict"]),
        tokens=row.get("tokens"),
        cost_usd=row.get("cost_usd"),
        duration_s=row.get("duration_s"),
        mcp_calls=row.get("mcp_calls"),
    )


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    bank: Bank,
    reference: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The run's figures per arm, beside the published arms on the same questions."""
    invalid = [
        {k: row[k] for k in ("arm", "question_id", "rep", "invalid")}
        for row in rows
        if row.get("invalid") or row.get("verdict") is None
    ]
    by_arm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("invalid") and row.get("verdict") is not None:
            by_arm[row["arm"]].append(row)
    arms: dict[str, Any] = {}
    for arm, arm_rows in sorted(by_arm.items()):
        figures = summarize([_ask(row) for row in arm_rows]).as_dict()
        repeats = [row for row in arm_rows if row.get("repeat_verdict")]
        figures["judge_agreement"] = (
            sum(row["repeat_verdict"] == row["verdict"] for row in repeats) / len(repeats)
            if repeats
            else None
        )
        figures["verdicts"] = {
            qid: "/".join(
                _LETTER[row["verdict"]]
                for row in sorted(arm_rows, key=lambda r: r["rep"])
                if row["question_id"] == qid
            )
            for qid in figures["by_question"]
        }
        arms[arm] = figures
    categories = {q.id: q.category for q in bank.questions}
    published = {}
    for arm in REFERENCE_ARMS:
        ref = summarize(reference_asks(reference, arm, categories))
        published[arm] = {
            "score": ref.score,
            "hallucination": ref.hallucination,
            "score_by_rep": ref.score_by_rep,
            "by_category": ref.by_category,
            "by_question": ref.by_question,
        }
    return _rounded(
        {
            "kind": "ctxbench-live",
            "suite": bank.suite,
            "version": bank.version,
            "content_hash": bank.content_hash,
            "questions": [q.id for q in bank.questions],
            "run": dict(run or {}),
            "valid": not invalid,
            "invalid": invalid,
            "arms": arms,
            "reference": published,
        }
    )


def _num(value: float | None, fmt: str = "{:.2f}") -> str:
    return "n/a" if value is None else fmt.format(value)


def render_markdown(summary: Mapping[str, Any]) -> str:
    """The report's tables: headline, by category, consistency, per question, lost points."""
    arms: Mapping[str, Any] = summary["arms"]
    reference: Mapping[str, Any] = summary["reference"]
    lines = [
        f"# ctxbench: {summary['suite']} {summary['version']}",
        "",
        f"Bank `{summary['content_hash']}`. Run valid: **{'yes' if summary['valid'] else 'no'}**"
        + (f" ({len(summary['invalid'])} invalid asks)." if summary["invalid"] else "."),
        "",
        "## Headline",
        "",
        "| Arm | Score (range) | Hallucinated | Tokens p50 / p95 | Cost per ask mean / median"
        " | Time p50 / p95 | MCP calls |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm, f in arms.items():
        reps = f["score_by_rep"]
        lines.append(
            f"| {arm} | {f['score']:.2f} ({min(reps):.2f}-{max(reps):.2f}) "
            f"| {f['hallucination']:.1%} "
            f"| {_num(f['tokens_p50'], '{:,.0f}')} / {_num(f['tokens_p95'], '{:,.0f}')} "
            f"| {_num(f['cost_mean'], '${:.3f}')} / {_num(f['cost_median'], '${:.3f}')} "
            f"| {_num(f['time_p50'], '{:.1f} s')} / {_num(f['time_p95'], '{:.1f} s')} "
            f"| {_num(f['calls_mean'], '{:.1f}')} |"
        )
    for arm, label in REFERENCE_ARMS.items():
        r = reference[arm]
        reps = r["score_by_rep"]
        lines.append(
            f"| {label} | {r['score']:.2f} ({min(reps):.2f}-{max(reps):.2f}) "
            f"| {r['hallucination']:.1%} | n/a | n/a | n/a | n/a |"
        )
    lines += ["", "## By category", ""]
    header = ["Category", *arms, *REFERENCE_ARMS.values()]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for category in CATEGORIES:
        cells = [_num(f["by_category"].get(category)) for f in arms.values()]
        cells += [_num(reference[arm]["by_category"].get(category)) for arm in REFERENCE_ARMS]
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Consistency",
        "",
        "| Arm | Same verdict on all asks | Median CV of tokens | Median CV of time"
        " | Judge agreement (ask 1 graded twice) |",
        "|---|---|---|---|---|",
    ]
    for arm, f in arms.items():
        lines.append(
            f"| {arm} | {f['same_verdict_share']:.0%} | {_num(f['cv_tokens_median'], '{:.0%}')} "
            f"| {_num(f['cv_time_median'], '{:.0%}')} | {_num(f['judge_agreement'], '{:.0%}')} |"
        )
    utopia = reference["utopia"]["by_question"]
    lines += ["", "## Per question", ""]
    header = ["Id", *(f"{arm} verdicts | {arm} score" for arm in arms), "Utopia", "Δ"]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * (2 * len(arms) + 3)]
    focus = "forge" if "forge" in arms else next(iter(arms), None)
    for qid in sorted(utopia):
        cells = []
        for f in arms.values():
            score = f["by_question"].get(qid)
            cells += [f["verdicts"].get(qid, "n/a"), _num(score)]
        mine = arms[focus]["by_question"].get(qid) if focus else None
        delta = "n/a" if mine is None else f"{mine - utopia[qid]:+.2f}"
        lines.append(f"| {qid} | " + " | ".join(cells) + f" | {utopia[qid]:.2f} | {delta} |")
    if focus:
        lost = {qid: 1 - s for qid, s in arms[focus]["by_question"].items() if s < 1}
        lines += [
            "",
            f"## Lost points ({focus})",
            "",
            "Cause, filled in by a person: T tracker/history not found, F part of a list or"
            " requirement, C code map, X documents disagree, S status, D document not held,"
            " G stopped early.",
            "",
            "| Id | Lost | Cause |",
            "|---|---|---|",
            *(f"| {qid} | {loss:.2f} | |" for qid, loss in sorted(lost.items())),
            f"| total | {sum(lost.values()):.2f} | |",
        ]
    return "\n".join(lines) + "\n"
