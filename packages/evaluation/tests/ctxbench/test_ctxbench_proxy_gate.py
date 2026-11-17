"""The evidence proxy (labels, never ids) and the gates' exit codes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from scf_eval.ctxbench.bank import load_bank
from scf_eval.ctxbench.gate import ACCEPTANCE, FLOOR, Bar, gate_live, gate_proxy
from scf_eval.ctxbench.proxy import ProxyError, evidence_matches, proxy_json, run_proxy

BANK = load_bank(subject="acme-example/quayline")
URI = "https://github.com/acme-example/quayline"


@pytest.mark.parametrize(
    ("evidence", "source", "hit"),
    [
        ("adrs/ADR-045-npm.md", f"repo:{URI}:adrs/ADR-045-npm.md", True),
        ("adrs/ADR-045-npm.md", f"repo:{URI}:old/adrs/ADR-045-npm.md", False),
        ("CLAUDE.md", f"{URI}:CLAUDE.md", True),  # an instruction item's label
        ("CLAUDE.md", f"repo:{URI}:docs/CLAUDE.md", False),
        ("libs/otel/", f"repo:{URI}:libs/otel/meter.ts", True),
        ("libs/otel/", f"repo:{URI}:libs/otelx/meter.ts", False),
        ("ca031776d", f"repo:{URI}:commits/ca031776d" + "0" * 31, True),
        ("ca031776d", f"repo:{URI}:commits/ba031776d" + "0" * 31, False),
        ("#2431", f"url:{URI}:pulls/2431", True),
        ("#2766", f"url:{URI}:issues/2766", True),
        ("#2431", f"url:{URI}:pulls/24310", False),
    ],
)
def test_evidence_matches_on_labels(evidence: str, source: str, hit: bool) -> None:
    assert evidence_matches(evidence, source) is hit


def _assembler(found: Mapping[str, list[str]], legs: list[str] | None = None) -> Any:
    by_text = {q.question: q.id for q in BANK.questions}

    def assemble(task: str) -> Mapping[str, Any]:
        qid = by_text[task]
        items = [{"id": "random-uuid", "source": s} for s in found.get(qid, [])]
        bundle = {"items": items, "total_tokens": 1000 + 10 * len(items), "legs": ["bm25"]}
        return {**bundle, "legs": legs} if legs is not None and qid == "scf-req-01" else bundle

    return assemble


def test_run_proxy_scores_hits_and_writes_stable_json() -> None:
    found = {
        "scf-dec-01": [f"repo:{URI}:docs/adr/0001-one-postgres-database.md"],
        "scf-hist-01": [f"repo:{URI}:commits/54d11fd16{'1' * 31}"],
        "scf-trk-01": [f"url:{URI}:issues/169"],
        "scf-code-01": [f"repo:{URI}:src/quayline/bookings/waitlist.py"],  # the tempting one
        "scf-none-01": [f"repo:{URI}:README.md"],
    }
    result = run_proxy(BANK, _assembler(found), commit="abc123")
    assert [qid for qid, hit in result.hits.items() if hit] == [
        "scf-dec-01",
        "scf-hist-01",
        "scf-trk-01",
    ]
    assert result.hits["scf-code-01"] is False
    assert result.hits["scf-none-01"] is None
    assert result.recall == pytest.approx(3 / 56)
    assert result.recall_by_category(BANK)["tracker"] == 1 / 8
    text = proxy_json(result, BANK)
    assert text == proxy_json(run_proxy(BANK, _assembler(found), commit="abc123"), BANK)
    assert "random-uuid" not in text
    stamped = json.loads(text)
    assert stamped["kind"] == "ctxbench-proxy"
    assert {k: stamped[k] for k in ("lane", "embedder", "commit")} == {
        "lane": "proxy",
        "embedder": "none",
        "commit": "abc123",
    }
    assert stamped["content_hash"] == BANK.content_hash
    assert stamped["corpus_hash"] == BANK.manifest["corpus_hash"]


@pytest.mark.parametrize("legs", [[], ["vector"], ["bm25", "vector"]])
def test_a_missing_or_unexpected_retrieval_leg_is_an_error_not_a_score(legs: list[str]) -> None:
    with pytest.raises(ProxyError, match=r"scf-req-01: retrieval legs"):
        run_proxy(BANK, _assembler({}, legs=legs))


def test_a_failing_assembly_is_an_error_not_a_score() -> None:
    def broken(task: str) -> Mapping[str, Any]:
        raise RuntimeError("bm25 index unavailable")

    with pytest.raises(ProxyError, match="scf-dec-01: assembly failed: RuntimeError: bm25 index"):
        run_proxy(BANK, broken)


def _proxy(hits: dict[str, bool | None], p50: float) -> dict[str, Any]:
    return {"kind": "ctxbench-proxy", "content_hash": "h", "hits": hits, "tokens_p50": p50}


def test_gate_proxy() -> None:
    base = _proxy({"a": True, "b": False, "c": None}, 1000)
    assert gate_proxy(_proxy({"a": True, "b": True, "c": None}, 1100), baseline=base).exit_code == 0
    lost = gate_proxy(_proxy({"a": False, "b": True, "c": None}, 900), baseline=base)
    assert (lost.exit_code, lost.reasons) == (1, ("a: evidence hit lost",))
    assert gate_proxy(_proxy({"a": True, "b": True, "c": None}, 1101), baseline=base).exit_code == 1
    other = gate_proxy({**_proxy({"a": True}, 1), "content_hash": "x"}, baseline=base)
    assert other.exit_code == 2
    assert gate_proxy({"kind": "something"}, baseline=base).exit_code == 2
    assert gate_proxy(base).exit_code == 0
    corpus = gate_proxy({**_proxy({"a": True}, 1000), "corpus_hash": "other"}, baseline=base)
    assert (corpus.exit_code, corpus.reasons) == (2, ("the baseline was taken on another corpus",))


def test_gate_proxy_holds_p50_bundle_tokens_to_the_ceiling() -> None:
    assert gate_proxy(_proxy({"a": True}, 17_800)).exit_code == 0
    over = gate_proxy(_proxy({"a": True}, 17_801))
    assert (over.exit_code, over.reasons) == (1, ("p50 bundle tokens 17801 above 17800",))
    base = _proxy({"a": True}, 17_000)
    assert gate_proxy(_proxy({"a": True}, 17_801), baseline=base).exit_code == 1


QUESTIONS = [f"q{n:02d}" for n in range(30)]


def _live(score: float, hallucination: float, p50: float | None, **top: Any) -> dict[str, Any]:
    arm = {
        "score": score,
        "hallucination": hallucination,
        "tokens_p50": p50,
        "verdicts": dict.fromkeys(QUESTIONS, "C/C/C"),
    }
    summary = {"kind": "ctxbench-live", "content_hash": "h", "valid": True, "questions": QUESTIONS}
    return {**summary, "arms": {"forge": arm}, **top}


@pytest.mark.parametrize(
    ("half_points", "bar", "exit_code"),
    [(154, FLOOR, 1), (155, FLOOR, 0), (165, ACCEPTANCE, 1), (166, ACCEPTANCE, 0)],
)
def test_gate_live_compares_the_score_unrounded(half_points: int, bar: Bar, exit_code: int) -> None:
    # 90 asks: 165/180 is Utopia's 27.5/30, which rounds to 0.92 and still misses the bar
    assert gate_live(_live(half_points / 180, 0.0, 17_000), bar=bar).exit_code == exit_code


@pytest.mark.parametrize(
    ("hallucination", "p50", "bar", "exit_code"),
    [
        (5 / 90, 17_800, ACCEPTANCE, 0),  # Utopia's 5.56% is under 5.6%
        (0.0564, 17_000, ACCEPTANCE, 1),  # rounds to 5.6%, and is over it
        (6 / 90, 17_000, ACCEPTANCE, 1),
        (6 / 90, 17_800, FLOOR, 0),
        (0.0704, 17_000, FLOOR, 1),  # rounds to 7.0%, and is over it
        (0.0, 17_801, FLOOR, 1),
        (0.0, 17_801, ACCEPTANCE, 1),
    ],
)
def test_gate_live_hallucination_and_token_limits(
    hallucination: float, p50: int, bar: Bar, exit_code: int
) -> None:
    assert gate_live(_live(1.0, hallucination, p50), bar=bar).exit_code == exit_code


def test_gate_live_floor_baseline_and_invalid() -> None:
    floor_miss = gate_live(_live(0.85, 0.08, 18_000), bar=FLOOR)
    assert floor_miss.exit_code == 1
    assert len(floor_miss.reasons) == 3
    regressed = gate_live(_live(0.88, 0.05, 17_000), baseline=_live(0.94, 0.05, 17_000))
    assert regressed.exit_code == 1
    assert gate_live(_live(0.90, 0.05, 17_000), baseline=_live(0.94, 0.05, 17_000)).exit_code == 0
    assert gate_live(_live(1.0, 0.0, 100, valid=False)).exit_code == 2
    assert gate_live(_live(1.0, 0.0, None)).exit_code == 2


@pytest.mark.parametrize(
    "baseline",
    [
        {"content_hash": "h"},
        {"content_hash": "h", "arms": {"none": {"score": 0.9}}},  # another arm only
        {"content_hash": "h", "arms": {"forge": {"hallucination": 0.0}}},  # no score
        _live(0.5, 0.0, 17_000, content_hash="another bank"),
    ],
)
def test_gate_live_refuses_a_baseline_it_cannot_compare_with(baseline: dict[str, Any]) -> None:
    good, miss = _live(0.95, 0.0, 17_000), _live(0.5, 0.0, 17_000)
    assert gate_live(good).exit_code == 0
    assert gate_live(good, baseline=baseline).exit_code == 2
    assert gate_live(miss, baseline=baseline).exit_code == 2  # invalid, not a bar miss


@pytest.mark.parametrize(
    "verdicts",
    [
        dict.fromkeys(QUESTIONS[1:], "C/C/C"),  # a question never asked
        {**dict.fromkeys(QUESTIONS, "C/C/C"), "q00": "C/C"},  # two asks
        {**dict.fromkeys(QUESTIONS, "C/C/C"), "q00": "C/C/C/C"},
        {**dict.fromkeys(QUESTIONS, "C/C/C"), "q99": "C/C/C"},  # not in the bank
    ],
)
def test_gate_live_passes_only_a_run_that_covers_the_bank(verdicts: dict[str, str]) -> None:
    run = _live(1.0, 0.0, 17_000)
    run["arms"]["forge"]["verdicts"] = verdicts
    assert gate_live(run, bar=ACCEPTANCE).exit_code == 2
    unlisted = _live(1.0, 0.0, 17_000)
    del unlisted["questions"]
    assert gate_live(unlisted).exit_code == 2
