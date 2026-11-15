"""Gates over a live summary or a proxy result: exit 0 pass, 1 fail, 2 invalid.

The live bar (spec J §2.4.1): J is accepted at score >= 0.92, hallucination
<= 5.6% and p50 tokens <= 17.8k in two consecutive live runs
(:data:`ACCEPTANCE`); a CI run fails under the hard floor 0.86 / 7% / 17.8k
(:data:`FLOOR`). The figures are compared as they are, never rounded to the
published precision: on 90 asks Utopia's 165/180 (0.9167) misses the bar and
166/180 meets it. A run also fails when its score falls more than ``noise``
(0.05, lore's noise level) below a committed baseline. The proxy fails when
any question's evidence hit goes from true to false, or p50 bundle tokens
rise more than 10% over the baseline.

An invalid run is never a pass: a run with invalid asks, one that does not
cover every question of the bank with :data:`ASKS_PER_QUESTION` asks, or a
baseline without the arm's score or taken on another bank.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "ACCEPTANCE",
    "ASKS_PER_QUESTION",
    "FLOOR",
    "Bar",
    "GateResult",
    "gate_live",
    "gate_proxy",
]

ASKS_PER_QUESTION = 3


@dataclass(frozen=True)
class Bar:
    min_score: float
    max_hallucination: float
    max_tokens_p50: int


ACCEPTANCE = Bar(min_score=0.92, max_hallucination=0.056, max_tokens_p50=17_800)
FLOOR = Bar(min_score=0.86, max_hallucination=0.07, max_tokens_p50=17_800)

_EXIT = {"pass": 0, "fail": 1, "invalid": 2}


@dataclass(frozen=True)
class GateResult:
    status: Literal["pass", "fail", "invalid"]
    reasons: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return _EXIT[self.status]


def _verdict(failures: list[str]) -> GateResult:
    return GateResult("fail", tuple(failures)) if failures else GateResult("pass")


def gate_live(
    summary: Mapping[str, Any],
    *,
    bar: Bar = FLOOR,
    baseline: Mapping[str, Any] | None = None,
    arm: str = "forge",
    noise: float = 0.05,
) -> GateResult:
    """Gate ``summary.json``'s ``arm`` against ``bar`` and an optional baseline summary."""
    if not summary.get("valid"):
        return GateResult("invalid", ("the run has invalid asks",))
    figures = (summary.get("arms") or {}).get(arm)
    if not figures or figures.get("tokens_p50") is None:
        return GateResult("invalid", (f"no {arm} arm with token figures in the summary",))
    asks = {qid: len(str(v).split("/")) for qid, v in (figures.get("verdicts") or {}).items()}
    expected = summary.get("questions")
    if not expected or asks != dict.fromkeys(expected, ASKS_PER_QUESTION):
        reason = f"the {arm} arm does not cover every question with {ASKS_PER_QUESTION} asks"
        return GateResult("invalid", (reason,))
    base = None
    if baseline is not None:
        base = ((baseline.get("arms") or {}).get(arm) or {}).get("score")
        if base is None:
            return GateResult("invalid", (f"the baseline has no {arm} score",))
        if baseline.get("content_hash") != summary.get("content_hash"):
            return GateResult("invalid", ("the baseline was taken on a different bank",))
    score, hallucination = figures["score"], figures["hallucination"]
    failures = []
    # shortcut: summary.json holds four decimals, which cannot flip a comparison on a bank
    # under 100 questions (the first is at 113); store more decimals before gating a larger one.
    if score < bar.min_score:
        failures.append(f"score {score:.4f} below {bar.min_score}")
    if hallucination > bar.max_hallucination:
        failures.append(f"hallucination {hallucination:.2%} above {bar.max_hallucination:.1%}")
    if figures["tokens_p50"] > bar.max_tokens_p50:
        failures.append(f"p50 tokens {figures['tokens_p50']:.0f} above {bar.max_tokens_p50}")
    if base is not None and score < base - noise:
        failures.append(f"score {score:.4f} regressed more than {noise} from {base:.4f}")
    return _verdict(failures)


def gate_proxy(
    result: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any] | None = None,
    token_slack: float = 0.10,
) -> GateResult:
    """Gate a ``proxy-result.json`` against a committed ``proxy-baseline.json``."""
    if result.get("kind") != "ctxbench-proxy" or not result.get("hits"):
        return GateResult("invalid", ("not a ctxbench proxy result",))
    if baseline is None:
        return GateResult("pass")
    if baseline.get("content_hash") != result.get("content_hash"):
        return GateResult("invalid", ("the baseline was taken on a different bank",))
    failures = [
        f"{qid}: evidence hit lost"
        for qid, hit in sorted(baseline["hits"].items())
        if hit and not result["hits"].get(qid)
    ]
    limit = baseline["tokens_p50"] * (1 + token_slack)
    if result["tokens_p50"] > limit:
        failures.append(f"p50 bundle tokens {result['tokens_p50']} above {limit:.1f}")
    return _verdict(failures)
