"""The Opus judge: rubric prompt in, one strict JSON verdict out (spec J §2.4.1).

The judge sees the question, the gold answer, whether the question is
answerable and the agent's answer; never its sources, tool output or
transcript (lore's "question, gold, answer"). It runs through the same
isolated ``claude --print`` wrapper as an ask, with no tools and no MCP
server (a run that names either is a failed run). Its reply must be exactly
``{"verdict": ..., "reason": ...}`` (one code fence around it is tolerated);
anything else is a :class:`JudgeError`, never a guessed verdict.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from forge_eval.ctxbench.bank import Question
from forge_eval.ctxbench.claude import (
    JUDGE_MODEL,
    ClaudeCall,
    ClaudeResult,
    TokenUsage,
    ask_problems,
)
from forge_eval.ctxbench.prompts import render

__all__ = [
    "VERDICTS",
    "JudgeError",
    "Judgement",
    "Verdict",
    "judge",
    "judge_prompt",
    "parse_verdict",
]

Verdict = Literal["correct", "partial", "wrong", "dont_know"]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)

_FENCED_RE = re.compile(r"```(?:json)?\s*\n(.*)\n```", re.DOTALL)


class JudgeError(ValueError):
    """The judge's reply is not exactly one valid verdict object."""


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    reason: str
    model: str
    usage: TokenUsage
    cost_usd: float


def judge_prompt(rubric: str, question: Question, answer: str) -> str:
    return render(
        rubric,
        {
            "question": question.question,
            "gold": question.gold,
            "answerable": "yes" if question.answerable else "no",
            "answer": answer,
        },
    )


def parse_verdict(text: str) -> tuple[Verdict, str]:
    """Strict: the reply is one JSON object with exactly ``verdict`` and ``reason``."""
    body = text.strip()
    fenced = _FENCED_RE.fullmatch(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        obj = json.loads(body)
    except ValueError as exc:
        raise JudgeError(f"judge reply is not JSON: {body[:80]!r}") from exc
    if not isinstance(obj, dict) or set(obj) != {"verdict", "reason"}:
        raise JudgeError("judge reply must be an object with exactly verdict and reason")
    verdict, reason = obj["verdict"], obj["reason"]
    if verdict not in VERDICTS or not isinstance(reason, str):
        raise JudgeError(f"judge verdict {verdict!r} is not one of {VERDICTS}")
    return cast(Verdict, verdict), reason


def judge(
    question: Question,
    answer: str,
    *,
    rubric: str,
    runner: Callable[[ClaudeCall], ClaudeResult],
    model: str = JUDGE_MODEL,
    claude_bin: str = "claude",
) -> Judgement:
    """Grade one answer; raises :class:`JudgeError` on a failed run or a bad reply."""
    call = ClaudeCall(
        model=model, prompt=judge_prompt(rubric, question, answer), claude_bin=claude_bin
    )
    result = runner(call)
    problems = ask_problems(result, expect_forge=False)  # no MCP server, no tool
    if problems:
        raise JudgeError(f"judge run failed: {'; '.join(problems)}")
    verdict, reason = parse_verdict(result.text)
    return Judgement(
        verdict=verdict, reason=reason, model=model, usage=result.usage, cost_usd=result.cost_usd
    )
