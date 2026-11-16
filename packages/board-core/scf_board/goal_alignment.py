"""Sprint-goal <-> acceptance-criteria alignment (F40 PM depth).

Pure keyword-overlap heuristic: a committed task is "aligned" with the sprint
goal when its title or acceptance criteria share a meaningful token with the
goal text. No embeddings/LLM call — that would be an infra-ceiling dependency
(a model call) out of the in-sandbox-testable delta — this is a deterministic,
explainable first pass the UI highlights gaps with; teams can still read every
unaligned task and decide for themselves. No I/O.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_STOPWORDS = frozenset(
    {
        "for",
        "the",
        "have",
        "not",
        "onto",
        "their",
        "are",
        "our",
        "into",
        "its",
        "should",
        "has",
        "with",
        "shall",
        "will",
        "and",
        "all",
        "from",
        "any",
        "were",
        "can",
        "was",
        "must",
        "goal",
        "this",
        "that",
        "sprint",
    }
)

_TOKEN_RE = re.compile("[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 3 and t not in _STOPWORDS}


class TaskAlignmentInput(BaseModel):
    """A committed task's alignment-relevant text."""

    task_id: str
    title: str = ""
    acceptance_criteria: list[str] = []


class GoalAlignmentResult(BaseModel):
    """The sprint goal's coverage across its committed tasks."""

    goal_tokens: list[str] = []
    total_count: int = 0
    aligned_count: int = 0
    alignment_ratio: float = 0.0
    unaligned_task_ids: list[str] = []


def compute_goal_alignment(
    goal: str | None, tasks: list[TaskAlignmentInput]
) -> GoalAlignmentResult:
    """Score how many of ``tasks`` share a meaningful token with ``goal``.

    No goal set, or a goal with no scoreable tokens (all stopwords/short
    words), yields a neutral zero-signal result — there is nothing to check
    tasks against, so none are flagged as unaligned.
    """
    goal_tokens = _tokens(goal if goal else "")
    total = len(tasks)
    if not goal_tokens or total == 0:
        return GoalAlignmentResult(total_count=total)

    unaligned: list[str] = []
    aligned = 0
    for t in tasks:
        haystack = " ".join([t.title, *t.acceptance_criteria])
        if _tokens(haystack) & goal_tokens:
            aligned += 1
        else:
            unaligned.append(t.task_id)

    return GoalAlignmentResult(
        goal_tokens=sorted(goal_tokens),
        total_count=total,
        aligned_count=aligned,
        alignment_ratio=round(aligned / total, 4),
        unaligned_task_ids=unaligned,
    )


__all__ = ["GoalAlignmentResult", "TaskAlignmentInput", "compute_goal_alignment"]
