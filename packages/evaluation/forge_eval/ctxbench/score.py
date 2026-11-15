"""Lore's scoring, made explicit (pure; spec J §2.4.1).

Per ask: correct 1, partial 0.5, wrong 0, dont_know 0. On an unanswerable
question, saying it does not know, or that no such thing exists (judged
``correct``), scores 1; partial and wrong score 0 (derived from lore's raw
data: lore-none-01/lore is A/P/C and scores 0.67). A question scores the mean
of its asks, an arm the mean of its questions; the range is each rep scored
alone. Hallucinated asks are wrong answers to answerable questions plus any
answer (partial or wrong) to an unanswerable one, over all asks; abstaining
is never a hallucination.

:func:`reference_asks` turns ``reference.json``'s published verdicts into
asks, so the same code reproduces lore's figures (the acceptance test).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from forge_eval.ctxbench.bank import CATEGORIES, Category
from forge_eval.ctxbench.judge import Verdict

__all__ = [
    "GradedAsk",
    "Summary",
    "ask_score",
    "hallucinated",
    "percentile",
    "reference_asks",
    "summarize",
]

_SCORE: dict[str, float] = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "dont_know": 0.0}


def ask_score(verdict: Verdict, *, answerable: bool) -> float:
    if answerable:
        return _SCORE[verdict]
    return 1.0 if verdict in ("correct", "dont_know") else 0.0


def hallucinated(verdict: Verdict, *, answerable: bool) -> bool:
    if answerable:
        return verdict == "wrong"
    return verdict in ("partial", "wrong")


@dataclass(frozen=True)
class GradedAsk:
    question_id: str
    category: Category
    rep: int
    verdict: Verdict
    tokens: int | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    mcp_calls: int | None = None

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"

    @property
    def score(self) -> float:
        return ask_score(self.verdict, answerable=self.answerable)


@dataclass(frozen=True)
class Summary:
    questions: int
    asks: int
    score: float
    score_by_rep: tuple[float, ...]
    hallucination: float
    by_category: dict[str, float]
    by_question: dict[str, float]
    same_verdict_share: float
    tokens_p50: float | None
    tokens_p95: float | None
    cost_mean: float | None
    cost_median: float | None
    time_p50: float | None
    time_p95: float | None
    calls_mean: float | None
    cv_tokens_median: float | None
    cv_time_median: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile (``p`` in 0..100); the median at 50."""
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _present(values: Sequence[float | int | None]) -> list[float] | None:
    """The values when every ask has one, else ``None`` (a reference arm has no tokens)."""
    if not values or any(v is None for v in values):
        return None
    return [float(cast(float, v)) for v in values]


def _cv_median(groups: Mapping[str, list[float | int | None]]) -> float | None:
    cvs = []
    for values in groups.values():
        present = _present(values)
        if present is None:
            return None
        mean = _mean(present)
        cvs.append(statistics.pstdev(present) / mean if len(present) > 1 and mean else 0.0)
    return statistics.median(cvs) if cvs else None


def summarize(asks: Sequence[GradedAsk]) -> Summary:
    """The arm's figures over its asks (every question's asks must be present)."""
    if not asks:
        raise ValueError("no graded asks")
    by_question: dict[str, list[GradedAsk]] = defaultdict(list)
    for ask in asks:
        by_question[ask.question_id].append(ask)
    question_score = {
        qid: _mean([a.score for a in group]) for qid, group in sorted(by_question.items())
    }
    category_of = {qid: group[0].category for qid, group in by_question.items()}
    by_category: dict[str, float] = {
        category: _mean([s for qid, s in question_score.items() if category_of[qid] == category])
        for category in CATEGORIES
        if category in category_of.values()
    }
    reps = sorted({a.rep for a in asks})
    score_by_rep = tuple(
        _mean(
            [
                _mean([a.score for a in group if a.rep == rep])
                for group in by_question.values()
                if any(a.rep == rep for a in group)
            ]
        )
        for rep in reps
    )
    tokens = _present([a.tokens for a in asks])
    costs = _present([a.cost_usd for a in asks])
    times = _present([a.duration_s for a in asks])
    calls = _present([a.mcp_calls for a in asks])
    same = [len({a.verdict for a in group}) == 1 for group in by_question.values()]
    return Summary(
        questions=len(by_question),
        asks=len(asks),
        score=_mean(list(question_score.values())),
        score_by_rep=score_by_rep,
        hallucination=sum(hallucinated(a.verdict, answerable=a.answerable) for a in asks)
        / len(asks),
        by_category=by_category,
        by_question=question_score,
        same_verdict_share=sum(same) / len(same),
        tokens_p50=percentile(tokens, 50) if tokens else None,
        tokens_p95=percentile(tokens, 95) if tokens else None,
        cost_mean=_mean(costs) if costs else None,
        cost_median=statistics.median(costs) if costs else None,
        time_p50=percentile(times, 50) if times else None,
        time_p95=percentile(times, 95) if times else None,
        calls_mean=_mean(calls) if calls else None,
        cv_tokens_median=_cv_median(
            {qid: [a.tokens for a in group] for qid, group in by_question.items()}
        ),
        cv_time_median=_cv_median(
            {qid: [a.duration_s for a in group] for qid, group in by_question.items()}
        ),
    )


def reference_asks(
    reference: Mapping[str, Any], arm: str, categories: Mapping[str, Category]
) -> list[GradedAsk]:
    """Asks from a published arm's ``"C/C/P"`` verdict strings in ``reference.json``."""
    letters: Mapping[str, str] = reference["verdicts"]
    return [
        GradedAsk(
            question_id=qid,
            category=categories[qid],
            rep=rep,
            verdict=cast(Verdict, letters[letter]),
        )
        for qid, verdicts in sorted(reference["arms"][arm]["verdicts"].items())
        for rep, letter in enumerate(verdicts.split("/"), start=1)
    ]
