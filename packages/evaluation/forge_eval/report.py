"""Human-readable rendering of evaluation scorecards."""

from __future__ import annotations

from forge_eval.harness import TaskScorecard
from forge_eval.runner import Scorecard

__all__ = ["format_ablation", "format_scorecard", "format_task_scorecard"]


def format_scorecard(card: Scorecard) -> str:
    """Render a scorecard as a fixed-width text report (CI/console friendly)."""
    ndcg_k = card.ndcg_k or card.k
    lines: list[str] = []
    lines.append(f"Golden retrieval eval — {card.num_cases} case(s), k={card.k}")
    lines.append("-" * 72)
    header = (
        f"{'case':<24}{'recall@' + str(card.k):>10}{'MRR':>8}"
        f"{'nDCG@' + str(ndcg_k):>10}{'hit':>6}{'':>4}"
    )
    lines.append(header)
    for r in card.results:
        flag = "ok" if r.passed else "X"
        lines.append(
            f"{r.case_id:<24}{r.recall_at_k:>10.3f}{r.reciprocal_rank:>8.3f}"
            f"{r.ndcg_at_k:>10.3f}{('Y' if r.hit else 'N'):>6}{flag:>4}"
        )
    lines.append("-" * 72)
    lines.append(
        f"mean recall@{card.k}={card.mean_recall_at_k:.3f}  "
        f"MRR={card.mean_mrr:.3f}  "
        f"nDCG@{ndcg_k}={card.mean_ndcg_at_k:.3f}  "
        f"hit_rate={card.hit_rate:.3f}  "
        f"passed={card.num_passed}/{card.num_cases}"
    )
    verdict = "PASS" if card.passed else "FAIL"
    gate = f"gate (recall@{card.k} >= {card.recall_threshold:.3f}"
    if card.ndcg_threshold > 0.0:
        gate += f", nDCG@{ndcg_k} >= {card.ndcg_threshold:.3f}"
    gate += f"): {verdict}"
    lines.append(gate)
    return "\n".join(lines)


def format_ablation(ablation: dict[str, Scorecard]) -> str:
    """Render a hybrid vs single-leg ablation table (proves fusion adds recall).

    ``ablation`` maps a leg name (``hybrid`` / ``vector_only`` / ``keyword_only``)
    to its :class:`Scorecard` scored over the *same* corpus + golden set.
    """
    lines: list[str] = []
    lines.append("Ablation — hybrid vs single-leg (same corpus + golden set)")
    lines.append("-" * 72)
    lines.append(f"{'leg':<16}{'recall@k':>12}{'MRR':>10}{'nDCG':>10}{'hit_rate':>12}")
    # Stable, meaningful ordering: full pipeline first, then each leg.
    order = ["hybrid", "vector_only", "keyword_only"]
    keys = [k for k in order if k in ablation] + [k for k in ablation if k not in order]
    for key in keys:
        card = ablation[key]
        lines.append(
            f"{key:<16}{card.mean_recall_at_k:>12.3f}{card.mean_mrr:>10.3f}"
            f"{card.mean_ndcg_at_k:>10.3f}{card.hit_rate:>12.3f}"
        )
    lines.append("-" * 72)
    if "hybrid" in ablation:
        legs = [
            ablation[leg].mean_recall_at_k
            for leg in ("vector_only", "keyword_only")
            if leg in ablation
        ]
        best_leg = max(legs) if legs else 0.0
        delta = ablation["hybrid"].mean_recall_at_k - best_leg
        verdict = "PASS" if delta >= 0.0 else "FAIL"
        lines.append(
            f"hybrid recall - best single leg = {delta:+.3f} (fusion adds recall: {verdict})"
        )
    return "\n".join(lines)


def format_task_scorecard(card: TaskScorecard) -> str:
    """Render a :class:`TaskScorecard` as a fixed-width text report."""
    lines: list[str] = []
    lines.append(f"Golden task eval — {card.num_tasks} task(s), k={card.k}")
    lines.append("-" * 72)
    header = f"{'task':<28}{'kind':<14}{'req_sat':>9}{'recall':>9}{'':>4}"
    lines.append(header)
    for r in card.results:
        flag = "ok" if r.passed else "X"
        recall = "-" if r.retrieval_recall is None else f"{r.retrieval_recall:.3f}"
        lines.append(
            f"{r.task_id:<28}{r.kind:<14}{r.requirement_satisfaction:>9.3f}{recall:>9}{flag:>4}"
        )
    lines.append("-" * 72)
    mean_recall = card.mean_retrieval_recall
    recall_str = "-" if mean_recall is None else f"{mean_recall:.3f}"
    lines.append(
        f"mean requirement satisfaction={card.mean_requirement_satisfaction:.3f}  "
        f"mean recall@{card.k}={recall_str}  "
        f"pass rate={card.pass_rate:.3f} ({card.num_passed}/{card.num_tasks})"
    )
    lines.append("by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(card.by_kind().items())))
    verdict = "PASS" if card.passed else "FAIL"
    lines.append(
        f"gate (pass_rate >= {card.pass_rate_threshold:.3f}, "
        f"req_sat >= {card.satisfaction_rate_threshold:.3f}): {verdict}"
    )
    return "\n".join(lines)
