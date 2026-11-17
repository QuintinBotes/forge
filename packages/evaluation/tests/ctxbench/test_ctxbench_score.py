"""ctxbench's scoring (lore's method, made explicit) and the live summary and report."""

from __future__ import annotations

import json
from typing import Any

import pytest

from scf_eval.ctxbench.bank import load_bank
from scf_eval.ctxbench.report import build_summary, render_markdown
from scf_eval.ctxbench.score import GradedAsk, ask_score, hallucinated, summarize

BANK = load_bank(subject="acme-example/quayline")
RUN = {"reps": 3, "forge_sha": "0123abc", "forge_dirty": False, "embedder": "local/minilm"}


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
    summary = build_summary(rows, bank=BANK, run=RUN)
    assert summary["valid"] is True
    forge = summary["arms"]["forge"]
    assert forge["score"] == 1.0  # "no such thing" (correct) scores 1 on an unanswerable
    assert forge["judge_agreement"] == 1.0
    assert forge["tokens_p50"] == 2000
    assert summary["arms"]["none"]["score"] == round(4 / 60, 4)
    assert "reference" not in summary
    markdown = render_markdown(summary)
    assert "SECRET-REASON" not in json.dumps(summary) + markdown
    assert "| forge | 1.00 (1.00-1.00) | 0.0% |" in markdown
    assert "| scf-temp-01 | C/C/C | 1.00 | A/A/A | 0.00 |" in markdown
    assert "Utopia" not in markdown and "published" not in markdown


def test_summary_and_report_state_what_was_measured() -> None:
    summary = build_summary(_rows("forge", "correct"), bank=BANK, run=RUN)
    stamps = ("lane", "content_hash", "corpus_hash", "commit", "embedder")
    assert {k: summary[k] for k in stamps} == {
        "lane": "live",
        "content_hash": BANK.content_hash,
        "corpus_hash": BANK.manifest["corpus_hash"],
        "commit": "0123abc",
        "embedder": "local/minilm",
    }
    dirty = build_summary(_rows("forge", "correct"), bank=BANK, run={**RUN, "forge_dirty": True})
    assert dirty["commit"] == "0123abc (dirty)"
    markdown = render_markdown(summary)
    assert f"Question set `{BANK.content_hash}`" in markdown
    assert f"corpus `{BANK.manifest['corpus_hash']}`" in markdown
    assert "commit `0123abc`, lane `live`, embedder `local/minilm`" in markdown
    assert "The score is a measurement: no pass bar is set yet (#249)." in markdown
    assert "Hallucination is held to at most 7.0%." in markdown


def test_each_category_scores_its_own_questions() -> None:
    by_kind = {"decision": "correct", "requirement": "partial", "code-location": "wrong"}
    rows = [
        {**row, "verdict": by_kind.get(row["category"], "dont_know")}
        for row in _rows("forge", "correct")
    ]
    forge = build_summary(rows, bank=BANK, run=RUN)["arms"]["forge"]
    # 10 decision at 1, 10 requirement at 0.5, the 4 unanswerable at 1 (abstaining is right)
    assert forge["score"] == round(19 / 60, 4)
    assert forge["by_category"] == {
        "decision": 1.0,
        "requirement": 0.5,
        "code-location": 0.0,
        "history": 0.0,
        "tracker": 0.0,
        "learned": 0.0,
        "temporal": 0.0,
        "unanswerable": 1.0,
    }
    markdown = render_markdown(build_summary(rows, bank=BANK, run=RUN))
    table = markdown.split("## By category")[1].split("##")[0]
    assert "| decision | 1.00 |\n| requirement | 0.50 |\n| code-location | 0.00 |\n" in table
    assert "| temporal | 0.00 |\n| unanswerable | 1.00 |\n" in table


def test_an_invalid_ask_makes_the_summary_invalid() -> None:
    rows = _rows("forge", "correct")
    rows[0] = {**rows[0], "verdict": None, "invalid": ["mcp server forge failed"]}
    summary = build_summary(rows, bank=BANK)
    assert summary["valid"] is False
    assert summary["invalid"] == [
        {
            "arm": "forge",
            "question_id": "scf-dec-01",
            "rep": 1,
            "invalid": ["mcp server forge failed"],
        }
    ]
