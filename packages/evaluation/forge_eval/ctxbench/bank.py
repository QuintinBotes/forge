"""The ctxbench question bank: load, validate, hash, ``{org}`` substitution (spec J §2.4.1).

A suite directory holds ``manifest.json`` (suite, version, snapshot, category
mix, ``content_hash``), ``questions.json`` and ``prompts/*.md``. The content
hash is the sha256 of canonical JSON over the questions and the prompts,
taken *before* substitution, so a frozen suite cannot drift: change anything
and the loader refuses it until a new version directory is written.

The repository owner never appears in a suite file. Questions and gold
answers carry ``{org}``, replaced at load time with the owner part of the
subject (``FORGE_CTXBENCH_SUBJECT``, ``<owner>/<repo>``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

__all__ = [
    "CATEGORIES",
    "SUBJECT_ENV",
    "SUITE_DIR",
    "Bank",
    "BankError",
    "Category",
    "Question",
    "load_bank",
    "subject_from_env",
    "suite_hash",
]

Category = Literal[
    "decision",
    "requirement",
    "code-location",
    "history",
    "tracker",
    "learned",
    "temporal",
    "unanswerable",
]
CATEGORIES: tuple[Category, ...] = get_args(Category)

SUBJECT_ENV = "FORGE_CTXBENCH_SUBJECT"

#: The frozen lore suite shipped with forge_eval.
SUITE_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "ctxbench-lore" / "1.0.0"

PROMPT_FILES = ("agent.md", "judge.md")

_SUBJECT_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
_PLACEHOLDER = "{org}"


class BankError(ValueError):
    """A suite that is malformed, drifted from its hash, or cannot be substituted."""


@dataclass(frozen=True)
class Question:
    id: str
    category: Category
    question: str
    gold: str
    gold_verbatim: bool
    evidence: tuple[str, ...]
    corpus: tuple[str, ...]

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"


@dataclass(frozen=True)
class Bank:
    suite: str
    version: str
    content_hash: str
    questions: tuple[Question, ...]
    manifest: Mapping[str, Any]

    def by_id(self) -> dict[str, Question]:
        return {q.id: q for q in self.questions}


def subject_from_env(env: Mapping[str, str] | None = None) -> str:
    """``<owner>/<repo>`` from ``FORGE_CTXBENCH_SUBJECT``; refuses when unset or malformed."""
    subject = (os.environ if env is None else env).get(SUBJECT_ENV, "").strip()
    if not _SUBJECT_RE.fullmatch(subject):
        raise BankError(f"{SUBJECT_ENV} must be set to <owner>/<repo>")
    return subject


def suite_hash(suite_dir: Path) -> str:
    """``sha256:`` of canonical JSON over ``questions.json`` and the prompts (pre-substitution)."""
    payload = {
        "questions": json.loads((suite_dir / "questions.json").read_text(encoding="utf-8")),
        "prompts": {
            name: (suite_dir / "prompts" / name).read_text(encoding="utf-8")
            for name in PROMPT_FILES
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _question(raw: Mapping[str, Any], owner: str) -> Question:
    try:
        qid, category = str(raw["id"]), raw["category"]
        text, gold = raw["question"], raw["gold"]
        evidence, corpus, verbatim = raw["evidence"], raw["corpus"], raw["gold_verbatim"]
    except KeyError as exc:
        raise BankError(f"question {raw.get('id')!r}: missing field {exc}") from exc
    if category not in CATEGORIES:
        raise BankError(f"question {qid!r}: unknown category {category!r}")
    if not (isinstance(text, str) and text and isinstance(gold, str) and gold):
        raise BankError(f"question {qid!r}: question and gold must be non-empty strings")
    if not isinstance(verbatim, bool):
        raise BankError(f"question {qid!r}: gold_verbatim must be a boolean")
    for name, values in (("evidence", evidence), ("corpus", corpus)):
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise BankError(f"question {qid!r}: {name} must be a list of strings")
    if (category == "unanswerable") != (not evidence):
        raise BankError(f"question {qid!r}: only an unanswerable question has no evidence")
    return Question(
        id=qid,
        category=cast(Category, category),
        question=text.replace(_PLACEHOLDER, owner),
        gold=gold.replace(_PLACEHOLDER, owner),
        gold_verbatim=verbatim,
        evidence=tuple(evidence),
        corpus=tuple(corpus),
    )


def load_bank(suite_dir: Path = SUITE_DIR, *, subject: str) -> Bank:
    """Load a suite: verify its hash, ids and category mix, then substitute ``{org}``."""
    if not _SUBJECT_RE.fullmatch(subject):
        raise BankError("subject must be <owner>/<repo>")
    manifest = json.loads((suite_dir / "manifest.json").read_text(encoding="utf-8"))
    recomputed = suite_hash(suite_dir)
    if manifest.get("content_hash") != recomputed:
        raise BankError(
            f"{suite_dir}: content hash {recomputed} does not match the manifest's "
            f"{manifest.get('content_hash')}; a frozen suite changes only by a new version"
        )
    raw = json.loads((suite_dir / "questions.json").read_text(encoding="utf-8"))
    if (raw.get("suite"), raw.get("version")) != (manifest.get("suite"), manifest.get("version")):
        raise BankError("questions.json and manifest.json name different suites")
    owner = subject.split("/", 1)[0]
    questions = tuple(_question(item, owner) for item in raw["questions"])
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise BankError("duplicate question ids")
    mix = dict(Counter(q.category for q in questions))
    if mix != {k: v for k, v in manifest.get("mix", {}).items() if v}:
        raise BankError(f"category mix {mix} differs from the manifest's {manifest.get('mix')}")
    return Bank(
        suite=str(manifest["suite"]),
        version=str(manifest["version"]),
        content_hash=recomputed,
        questions=questions,
        manifest=manifest,
    )
