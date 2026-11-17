"""The frozen ctxbench-scf bank and corpus: hashes, mix, ``{org}`` substitution, prompts."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scf_eval.ctxbench.bank import (
    SUITE_DIR,
    BankError,
    load_bank,
    load_corpus,
    subject_from_env,
    suite_hash,
)
from scf_eval.ctxbench.prompts import load_prompts, parse_agent_reply, render

SUBJECT = "acme-example/quayline"


def _rehash(suite: Path, data: dict[str, Any]) -> None:
    """Write ``questions.json`` and a manifest whose hash matches it."""
    (suite / "questions.json").write_text(json.dumps(data), encoding="utf-8")
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = suite_hash(suite)
    (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_bank_loads_sixty_questions_in_its_mix() -> None:
    bank = load_bank(subject=SUBJECT)
    assert (bank.suite, bank.version) == ("ctxbench-scf", "1.0.0")
    assert len(bank.questions) == 60
    assert Counter(q.category for q in bank.questions) == {
        "decision": 10,
        "requirement": 10,
        "code-location": 8,
        "history": 8,
        "tracker": 8,
        "learned": 6,
        "temporal": 6,
        "unanswerable": 4,
    }
    assert bank.content_hash == bank.manifest["content_hash"] == suite_hash(SUITE_DIR)
    unanswerable = [q.id for q in bank.questions if not q.answerable]
    assert unanswerable == [f"scf-none-0{n}" for n in range(1, 5)]
    assert all(q.evidence for q in bank.questions if q.answerable)
    assert sum(len(q.evidence) > 1 for q in bank.questions) >= 20  # evidence across documents


def test_every_evidence_entry_names_something_in_the_corpus() -> None:
    records = load_corpus(SUITE_DIR)
    paths = {r["path"] for r in records if r["kind"] == "file"}
    shas = [r["sha"] for r in records if r["kind"] == "commit"]
    numbers = {r["number"] for r in records if r["kind"] in ("issue", "pull")}
    for q in load_bank(subject=SUBJECT).questions:
        for evidence in q.evidence:
            if evidence.startswith("#"):
                assert int(evidence[1:]) in numbers, (q.id, evidence)
            elif re.fullmatch(r"[0-9a-f]{7,40}", evidence):
                assert sum(s.startswith(evidence) for s in shas) == 1, (q.id, evidence)
            else:
                assert evidence in paths, (q.id, evidence)


def test_the_committed_corpus_loads_and_a_changed_one_is_refused(tmp_path: Path) -> None:
    records = load_corpus(SUITE_DIR)
    kinds = Counter(r["kind"] for r in records)
    assert kinds == {"file": 33, "commit": 174, "issue": 137, "pull": 44}
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite)
    corpus = suite / "corpus.jsonl"
    text = corpus.read_text(encoding="utf-8")
    corpus.write_text(text.replace("15 minutes", "20 minutes", 1), encoding="utf-8")
    with pytest.raises(BankError, match="corpus hash"):
        load_corpus(suite)
    load_bank(suite, subject=SUBJECT)  # the questions still load; only the corpus is refused


def test_org_placeholder_is_substituted_from_the_subject(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite)
    data = json.loads((suite / "questions.json").read_text(encoding="utf-8"))
    data["questions"][0].update(question="Does {org} run Quayline?", gold="Yes, {org} does.")
    _rehash(suite, data)
    first = load_bank(suite, subject=SUBJECT).questions[0]
    assert first.question == "Does acme-example run Quayline?"
    assert first.gold == "Yes, acme-example does."


def test_subject_is_required_and_validated() -> None:
    with pytest.raises(BankError):
        subject_from_env({})
    with pytest.raises(BankError):
        subject_from_env({"FORGE_CTXBENCH_SUBJECT": "no-slash"})
    assert subject_from_env({"FORGE_CTXBENCH_SUBJECT": SUBJECT}) == SUBJECT
    with pytest.raises(BankError):
        load_bank(subject="../x")


def test_a_changed_bank_or_prompt_no_longer_loads(tmp_path: Path) -> None:
    changes = (("questions.json", "15 minutes", "20 minutes"), ("prompts/judge.md", "\n", " \n"))
    for target, old, new in changes:
        suite = tmp_path / target.replace("/", "-")
        shutil.copytree(SUITE_DIR, suite)
        path = suite / target
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        with pytest.raises(BankError, match="content hash"):
            load_bank(suite, subject=SUBJECT)


def test_ids_mix_and_evidence_are_validated(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite)
    original = (suite / "questions.json").read_text(encoding="utf-8")

    def refused(match: str, change: Any) -> None:
        data = json.loads(original)
        change(data["questions"])
        _rehash(suite, data)
        with pytest.raises(BankError, match=match):
            load_bank(suite, subject=SUBJECT)

    refused("evidence", lambda qs: qs[0].update(evidence=[]))  # answerable, no evidence
    refused("evidence", lambda qs: qs[-1].update(evidence=["#17"]))  # unanswerable with evidence
    refused("duplicate", lambda qs: qs[1].update(id=qs[0]["id"]))
    refused("mix", lambda qs: qs.pop(0))


def test_prompts_render_once_and_the_agent_reply_parses_leniently() -> None:
    prompts = load_prompts(SUITE_DIR)
    agent = render(prompts.agent, {"subject": SUBJECT, "question": "Q? {gold}"})
    assert f"repository {SUBJECT}." in agent
    assert agent.rstrip().endswith("Question: Q? {gold}")  # a value is never expanded again
    assert '{"answer": "<your answer>"' in agent
    assert parse_agent_reply('```json\n{"answer": "a", "sources": ["s"]}\n```') == ("a", ["s"])
    assert parse_agent_reply('Here: {"answer": "b"} done') == ("b", [])
    assert parse_agent_reply("I don't know.") == ("I don't know.", [])
