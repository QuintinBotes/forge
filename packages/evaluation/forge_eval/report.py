"""Human-readable rendering of an evaluation :class:`Scorecard`."""

from __future__ import annotations

from forge_eval.runner import Scorecard

__all__ = ["format_scorecard"]


def format_scorecard(card: Scorecard) -> str:
    """Render a scorecard as a fixed-width text report (CI/console friendly)."""
    lines: list[str] = []
    lines.append(f"Golden retrieval eval — {card.num_cases} case(s), k={card.k}")
    lines.append("-" * 64)
    header = f"{'case':<24}{'recall@' + str(card.k):>10}{'MRR':>8}{'hit':>6}{'':>4}"
    lines.append(header)
    for r in card.results:
        flag = "ok" if r.passed else "X"
        lines.append(
            f"{r.case_id:<24}{r.recall_at_k:>10.3f}{r.reciprocal_rank:>8.3f}"
            f"{('Y' if r.hit else 'N'):>6}{flag:>4}"
        )
    lines.append("-" * 64)
    lines.append(
        f"mean recall@{card.k}={card.mean_recall_at_k:.3f}  "
        f"MRR={card.mean_mrr:.3f}  "
        f"hit_rate={card.hit_rate:.3f}  "
        f"passed={card.num_passed}/{card.num_cases}"
    )
    verdict = "PASS" if card.passed else "FAIL"
    lines.append(f"gate (recall@{card.k} >= {card.recall_threshold:.3f}): {verdict}")
    return "\n".join(lines)
