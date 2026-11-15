"""The model-free proxy: does ``assemble_context(question)`` return the gold evidence?

Pure over ``AssembleOut``-shaped dicts (spec J §2.4.1, CI lane). Evidence is
compared on each item's source label (``kind:uri:path`` for a passage,
``uri:path`` for an instruction file), never on ids, which differ between
ingests. Evidence entries are written as the article writes them:

* a repository path (``adrs/ADR-045-....md``); one ending in ``/`` is a directory;
* a commit sha prefix (``ca031776d``), matching ``commits/<sha>``;
* ``#N``, matching ``issues/N`` or ``pulls/N`` (one number space on GitHub).

A question hits when any of its evidence is in the bundle; an unanswerable
question has no evidence and no hit (``None``). The result holds only ids,
booleans and numbers, so it can be committed as a baseline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forge_eval.ctxbench.bank import CATEGORIES, Bank, Question
from forge_eval.ctxbench.score import percentile

__all__ = ["ProxyResult", "evidence_hit", "evidence_matches", "proxy_json", "run_proxy"]

_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_NUMBER_RE = re.compile(r"#(\d+)")


def evidence_matches(evidence: str, source: str) -> bool:
    """Whether a bundle item labelled ``source`` is the ``evidence`` entry."""
    number = _NUMBER_RE.fullmatch(evidence)
    if number:
        n = number.group(1)
        return source.endswith((f":issues/{n}", f":pulls/{n}"))
    if _SHA_RE.fullmatch(evidence):
        return re.search(rf":commits/{evidence}[0-9a-f]*$", source) is not None
    if evidence.endswith("/"):
        return f":{evidence}" in source
    return source.endswith(f":{evidence}")


def evidence_hit(question: Question, items: Sequence[Mapping[str, Any]]) -> bool | None:
    if not question.evidence:
        return None
    sources = [str(item.get("source", "")) for item in items]
    return any(evidence_matches(e, s) for e in question.evidence for s in sources)


@dataclass(frozen=True)
class ProxyResult:
    suite: str
    version: str
    content_hash: str
    hits: dict[str, bool | None]
    bundle_tokens: dict[str, int]

    @property
    def recall(self) -> float:
        scored = [hit for hit in self.hits.values() if hit is not None]
        return sum(scored) / len(scored) if scored else 0.0

    def recall_by_category(self, bank: Bank) -> dict[str, float]:
        out: dict[str, float] = {}
        for category in CATEGORIES:
            scored = [
                self.hits[q.id]
                for q in bank.questions
                if q.category == category and self.hits.get(q.id) is not None
            ]
            if scored:
                out[category] = sum(bool(hit) for hit in scored) / len(scored)
        return out

    @property
    def tokens_p50(self) -> float:
        return percentile(list(self.bundle_tokens.values()), 50)


def run_proxy(bank: Bank, assemble: Callable[[str], Mapping[str, Any]]) -> ProxyResult:
    """Assemble a bundle per question (in bank order) and score its evidence."""
    hits: dict[str, bool | None] = {}
    tokens: dict[str, int] = {}
    for question in bank.questions:
        bundle = assemble(question.question)
        hits[question.id] = evidence_hit(question, bundle.get("items") or [])
        tokens[question.id] = int(bundle.get("total_tokens") or 0)
    return ProxyResult(
        suite=bank.suite,
        version=bank.version,
        content_hash=bank.content_hash,
        hits=hits,
        bundle_tokens=tokens,
    )


def proxy_json(result: ProxyResult, bank: Bank) -> str:
    """Byte-stable JSON (sorted keys, fixed rounding, trailing newline)."""
    payload = {
        "kind": "ctxbench-proxy",
        "suite": result.suite,
        "version": result.version,
        "content_hash": result.content_hash,
        "hits": result.hits,
        "bundle_tokens": result.bundle_tokens,
        "recall": round(result.recall, 4),
        "recall_by_category": {k: round(v, 4) for k, v in result.recall_by_category(bank).items()},
        "tokens_p50": round(result.tokens_p50, 1),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
