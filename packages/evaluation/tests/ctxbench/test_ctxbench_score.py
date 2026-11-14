"""Lore's scoring, and the acceptance test: reproduce the published lore-30 figures.

From ``reference.json`` alone, ``score.py`` must give the figures the port
design (§1) recomputes from lore's per-question verdicts: Utopia 0.92
(27.5/30) with 5.6% hallucination (5 W / 90), Lore 0.57 (17.17/30) with 8.9%
(7 W + 1 P on an unanswerable / 90), no MCP 0.08 (2.5/30) with 0%, the
per-kind table, and Utopia's evening and night runs at 0.89 and 0.90.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from forge_eval.ctxbench.bank import SUITE_DIR, load_bank
from forge_eval.ctxbench.report import build_summary, render_markdown
from forge_eval.ctxbench.score import (
    GradedAsk,
    ask_score,
    hallucinated,
    reference_asks,
    summarize,
)

REFERENCE = json.loads((SUITE_DIR / "reference.json").read_text(encoding="utf-8"))
BANK = load_bank(subject="acme-example/lore")
CATEGORIES = {q.id: q.category for q in BANK.questions}

# design §1 "Per kind on the lore 30": (Utopia afternoon, Lore, no MCP)
PER_KIND = {
    "decision": (0.87, 0.97, 0.0),
    "requirement": (1.00, 0.37, 0.0),
    "code-location": (0.79, 0.33, 0.0),
    "history": (1.00, 0.83, 0.0),
    "tracker": (1.00, 0.00, 0.0),
    "learned": (1.00, 0.89, 0.17),
    "temporal": (0.67, 0.50, 0.0),
    "unanswerable": (1.00, 0.83, 1.00),
}


@pytest.mark.parametrize(
    ("arm", "total", "score", "hallucinated_asks", "rate"),
    [
        ("utopia", 27.5, 0.92, 5, 5.6),
        ("lore", 17.0 + 1 / 6, 0.57, 8, 8.9),
        ("none", 2.5, 0.08, 0, 0.0),
    ],
)
def test_reference_figures_are_reproduced(
    arm: str, total: float, score: float, hallucinated_asks: int, rate: float
) -> None:
    summary = summarize(reference_asks(REFERENCE, arm, CATEGORIES))
    assert (summary.questions, summary.asks) == (30, 90)
    assert summary.score * 30 == pytest.approx(total)
    assert round(summary.score, 2) == score
    assert summary.hallucination * 90 == pytest.approx(hallucinated_asks)
    assert round(summary.hallucination * 100, 1) == rate
    column = {"utopia": 0, "lore": 1, "none": 2}[arm]
    assert {k: round(v, 2) for k, v in summary.by_category.items()} == {
        kind: values[column] for kind, values in PER_KIND.items()
    }
    assert summary.tokens_p50 is None  # the reference holds verdicts only


def test_lore_none_01_is_how_unanswerable_scoring_was_derived() -> None:
    lore = summarize(reference_asks(REFERENCE, "lore", CATEGORIES))
    assert REFERENCE["arms"]["lore"]["verdicts"]["lore-none-01"] == "A/P/C"
    assert lore.by_question["lore-none-01"] == pytest.approx(2 / 3)


def test_utopia_evening_and_night_scores() -> None:
    for arm, expected in (("utopia_evening", 0.89), ("utopia_night", 0.90)):
        scores = REFERENCE["question_scores"][arm]["scores"]
        assert len(scores) == 30
        assert round(sum(scores.values()) / 30, 2) == expected


def test_ask_rules() -> None:
    assert [
        ask_score(v, answerable=True) for v in ("correct", "partial", "wrong", "dont_know")
    ] == [
        1.0,
        0.5,
        0.0,
        0.0,
    ]
    assert [
        ask_score(v, answerable=False) for v in ("correct", "partial", "wrong", "dont_know")
    ] == [1.0, 0.0, 0.0, 1.0]
    assert [
        hallucinated(v, answerable=True) for v in ("correct", "partial", "wrong", "dont_know")
    ] == [
        False,
        False,
        True,
        False,
    ]
    assert [
        hallucinated(v, answerable=False) for v in ("correct", "partial", "wrong", "dont_know")
    ] == [False, True, True, False]


def test_tokens_consistency_and_range() -> None:
    asks = [
        GradedAsk(
            "q1", "decision", 1, "correct", tokens=100, cost_usd=0.1, duration_s=2, mcp_calls=1
        ),
        GradedAsk(
            "q1", "decision", 2, "correct", tokens=100, cost_usd=0.1, duration_s=4, mcp_calls=1
        ),
        GradedAsk(
            "q1", "decision", 3, "partial", tokens=100, cost_usd=0.1, duration_s=6, mcp_calls=1
        ),
        GradedAsk("q2", "tracker", 1, "wrong", tokens=300, cost_usd=0.3, duration_s=2, mcp_calls=3),
        GradedAsk("q2", "tracker", 2, "wrong", tokens=500, cost_usd=0.5, duration_s=2, mcp_calls=3),
        GradedAsk("q2", "tracker", 3, "wrong", tokens=400, cost_usd=0.4, duration_s=2, mcp_calls=3),
    ]
    s = summarize(asks)
    assert s.score == pytest.approx((5 / 6 + 0) / 2)
    assert s.score_by_rep == pytest.approx((0.5, 0.5, 0.25))
    assert s.hallucination == pytest.approx(3 / 6)
    assert s.tokens_p50 == 200  # median of 100,100,100,300,400,500
    assert s.calls_mean == 2
    assert s.same_verdict_share == 0.5
    assert s.cv_tokens_median == pytest.approx((0 + (200 / 3) ** 0.5 * 10 / 400) / 2)


def _rows(arm: str, verdict: str, **extra: Any) -> list[dict[str, Any]]:
    return [
        {
            "arm": arm,
            "question_id": q.id,
            "category": q.category,
            "rep": rep,
            "verdict": verdict,
            "reason": "SECRET-REASON",
            "repeat_verdict": verdict if rep == 1 else None,
            "invalid": [],
            "tokens": 1000 * rep,
            "cost_usd": 0.01,
            "duration_s": 3.0,
            "mcp_calls": 1,
            **extra,
        }
        for q in BANK.questions
        for rep in (1, 2, 3)
    ]


def test_summary_and_markdown_hold_numbers_and_verdicts_only() -> None:
    rows = _rows("forge", "correct") + _rows("none", "dont_know")
    summary = build_summary(rows, bank=BANK, reference=REFERENCE, run={"reps": 3})
    assert summary["valid"] is True
    forge = summary["arms"]["forge"]
    assert forge["score"] == 1.0  # "no such thing" (correct) scores 1 on an unanswerable
    assert forge["judge_agreement"] == 1.0
    assert forge["tokens_p50"] == 2000
    assert summary["arms"]["none"]["score"] == round(2 / 30, 4)
    assert summary["reference"]["utopia"]["score"] == round(27.5 / 30, 4)
    markdown = render_markdown(summary)
    assert "SECRET-REASON" not in json.dumps(summary) + markdown
    assert "| forge | 1.00 (1.00-1.00) | 0.0% |" in markdown
    assert "| Utopia (published) | 0.92 (0.88-0.95) | 5.6% |" in markdown
    assert "| lore-temp-01 | C/C/C | 1.00 | A/A/A | 0.00 | 0.00 | +1.00 |" in markdown


def test_an_invalid_ask_makes_the_summary_invalid() -> None:
    rows = _rows("forge", "correct")
    rows[0] = {**rows[0], "verdict": None, "invalid": ["mcp server forge failed"]}
    summary = build_summary(rows, bank=BANK, reference=REFERENCE)
    assert summary["valid"] is False
    assert summary["invalid"] == [
        {
            "arm": "forge",
            "question_id": "lore-dec-01",
            "rep": 1,
            "invalid": ["mcp server forge failed"],
        }
    ]
