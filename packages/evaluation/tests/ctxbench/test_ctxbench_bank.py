"""The frozen ctxbench-lore bank: hash, mix, ``{org}`` substitution, prompts."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from forge_eval.ctxbench.bank import (
    SUITE_DIR,
    BankError,
    load_bank,
    subject_from_env,
    suite_hash,
)
from forge_eval.ctxbench.prompts import load_prompts, parse_agent_reply, render

SUBJECT = "acme-example/lore"


def test_bank_loads_thirty_questions_in_lores_mix() -> None:
    bank = load_bank(subject=SUBJECT)
    assert (bank.suite, bank.version) == ("ctxbench-lore", "1.0.0")
    assert len(bank.questions) == 30
    assert Counter(q.category for q in bank.questions) == {
        "decision": 5,
        "requirement": 5,
        "code-location": 4,
        "history": 4,
        "tracker": 4,
        "learned": 3,
        "temporal": 3,
        "unanswerable": 2,
    }
    assert bank.content_hash == bank.manifest["content_hash"] == suite_hash(SUITE_DIR)
    assert all(q.gold_verbatim for q in bank.questions)
    assert [q.id for q in bank.questions if not q.answerable] == ["lore-none-01", "lore-none-02"]
    assert all(q.evidence for q in bank.questions if q.answerable)


def test_org_placeholder_is_substituted_from_the_subject() -> None:
    raw = (SUITE_DIR / "questions.json").read_text(encoding="utf-8")
    assert raw.count("{org}") == 4  # dec-01's question, dec-03's gold twice, hist-01's gold
    bank = load_bank(subject=SUBJECT).by_id()
    assert bank["lore-dec-01"].question.startswith("How does a acme-example npm package")
    assert "@acme-example/floor-client" in bank["lore-dec-03"].gold
    assert not any("{org}" in q.question + q.gold for q in bank.values())


def test_the_bank_is_json_and_keeps_text_lore_lost_to_yaml_comments() -> None:
    gold = load_bank(subject=SUBJECT).by_id()["lore-hist-04"].gold
    assert gold.endswith("which added backend lore | utopia | both.")  # table escape undone
    assert "#2787" in gold  # an unquoted ` #` would have ended a YAML scalar here


def test_subject_is_required_and_validated() -> None:
    with pytest.raises(BankError):
        subject_from_env({})
    with pytest.raises(BankError):
        subject_from_env({"FORGE_CTXBENCH_SUBJECT": "no-slash"})
    assert subject_from_env({"FORGE_CTXBENCH_SUBJECT": SUBJECT}) == SUBJECT
    with pytest.raises(BankError):
        load_bank(subject="../x")


def test_a_changed_bank_or_prompt_no_longer_loads(tmp_path: Path) -> None:
    for target, old, new in (("questions.json", "0.92", "0.93"), ("prompts/judge.md", "\n", " \n")):
        suite = tmp_path / target.replace("/", "-")
        shutil.copytree(SUITE_DIR, suite)
        path = suite / target
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        with pytest.raises(BankError, match="content hash"):
            load_bank(suite, subject=SUBJECT)


def test_mix_and_evidence_are_validated(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite)
    data = json.loads((suite / "questions.json").read_text(encoding="utf-8"))
    data["questions"][0]["evidence"] = []  # an answerable question without evidence
    (suite / "questions.json").write_text(json.dumps(data), encoding="utf-8")
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = suite_hash(suite)
    (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BankError, match="evidence"):
        load_bank(suite, subject=SUBJECT)
    data["questions"] = data["questions"][1:]
    data["questions"][0]["evidence"] = ["x"]
    (suite / "questions.json").write_text(json.dumps(data), encoding="utf-8")
    manifest["content_hash"] = suite_hash(suite)
    (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BankError, match="mix"):
        load_bank(suite, subject=SUBJECT)


def test_prompts_render_once_and_the_agent_reply_parses_leniently() -> None:
    prompts = load_prompts(SUITE_DIR)
    agent = render(prompts.agent, {"subject": SUBJECT, "question": "Q? {gold}"})
    assert f"repository {SUBJECT}." in agent
    assert agent.rstrip().endswith("Question: Q? {gold}")  # a value is never expanded again
    assert '{"answer": "<your answer>"' in agent
    assert parse_agent_reply('```json\n{"answer": "a", "sources": ["s"]}\n```') == ("a", ["s"])
    assert parse_agent_reply('Here: {"answer": "b"} done') == ("b", [])
    assert parse_agent_reply("I don't know.") == ("I don't know.", [])
