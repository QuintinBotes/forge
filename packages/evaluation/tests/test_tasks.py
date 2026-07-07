"""Unit tests for the golden *task* set model + loader (Task 1.16).

A golden task is a representative engineering-task input paired with its
known-good output (the requirements it must satisfy, terminal status, the
verification checks it must pass, and — where relevant — the retrieval chunks it
should surface). These tests pin the loader's validation and the shipped V1
golden set (>=30 tasks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_eval.tasks import (
    GoldenRequirement,
    GoldenTask,
    load_golden_tasks,
    parse_golden_tasks,
)

V1_SET = Path(__file__).resolve().parent.parent / "forge_eval" / "golden" / "v1_task_set.yaml"


# --------------------------------------------------------------------------- #
# Shipped V1 golden task set                                                   #
# --------------------------------------------------------------------------- #


def test_v1_golden_set_has_at_least_30_tasks() -> None:
    tasks = load_golden_tasks(V1_SET)
    assert len(tasks) >= 30


def test_v1_golden_set_ids_unique_and_well_formed() -> None:
    tasks = load_golden_tasks(V1_SET)
    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids))
    for task in tasks:
        # Every golden task must carry at least one gradeable dimension.
        assert task.requirements or task.expected_chunks
        assert task.objective


def test_v1_golden_set_kinds_match_frozen_contract() -> None:
    from forge_contracts import TaskKind

    valid = {k.value for k in TaskKind}
    tasks = load_golden_tasks(V1_SET)
    assert all(t.kind in valid for t in tasks)


def test_v1_golden_set_has_core_and_stretch_requirements() -> None:
    # The reference baseline only solves core requirements, so the set must
    # contain at least one stretch requirement for the gate to be meaningful.
    tasks = load_golden_tasks(V1_SET)
    difficulties = {r.difficulty for t in tasks for r in t.requirements}
    assert "core" in difficulties
    assert "stretch" in difficulties


# --------------------------------------------------------------------------- #
# Parsing / validation                                                         #
# --------------------------------------------------------------------------- #


def test_parse_tasks_accepts_mapping_with_tasks_key() -> None:
    payload = {
        "tasks": [
            {
                "id": "T1",
                "objective": "add pagination",
                "kind": "feature",
                "requirements": ["R1", "R2"],
            }
        ]
    }
    tasks = parse_golden_tasks(payload)
    assert len(tasks) == 1
    assert tasks[0].requirement_ids == ["R1", "R2"]


def test_string_requirement_shorthand_is_core() -> None:
    tasks = parse_golden_tasks([{"id": "T1", "objective": "x", "requirements": ["R1"]}])
    assert tasks[0].requirements[0] == GoldenRequirement(id="R1")
    assert tasks[0].requirements[0].difficulty == "core"


def test_dict_requirement_carries_text_and_difficulty() -> None:
    tasks = parse_golden_tasks(
        [
            {
                "id": "T1",
                "objective": "x",
                "requirements": [{"id": "R1", "text": "must do thing", "difficulty": "stretch"}],
            }
        ]
    )
    req = tasks[0].requirements[0]
    assert req.text == "must do thing"
    assert req.difficulty == "stretch"


def test_core_requirement_ids_excludes_stretch() -> None:
    tasks = parse_golden_tasks(
        [
            {
                "id": "T1",
                "objective": "x",
                "requirements": [
                    {"id": "R1"},
                    {"id": "R2", "difficulty": "stretch"},
                ],
            }
        ]
    )
    task = tasks[0]
    assert task.requirement_ids == ["R1", "R2"]
    assert task.core_requirement_ids == ["R1"]


def test_retrieval_only_task_is_valid() -> None:
    tasks = parse_golden_tasks([{"id": "T1", "objective": "find auth", "expected_chunks": ["c1"]}])
    assert tasks[0].expected_chunks == ["c1"]
    assert tasks[0].requirements == []


def test_task_without_any_gradeable_dimension_is_rejected() -> None:
    with pytest.raises(ValueError, match="requirements"):
        parse_golden_tasks([{"id": "T1", "objective": "x"}])


def test_duplicate_task_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_golden_tasks(
            [
                {"id": "T1", "objective": "a", "requirements": ["R1"]},
                {"id": "T1", "objective": "b", "requirements": ["R2"]},
            ]
        )


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        parse_golden_tasks(
            [{"id": "T1", "objective": "x", "kind": "nonsense", "requirements": ["R1"]}]
        )


def test_invalid_difficulty_rejected() -> None:
    with pytest.raises(ValueError, match="difficulty"):
        parse_golden_tasks(
            [
                {
                    "id": "T1",
                    "objective": "x",
                    "requirements": [{"id": "R1", "difficulty": "impossible"}],
                }
            ]
        )


def test_missing_required_field_rejected() -> None:
    with pytest.raises(ValueError, match="objective"):
        parse_golden_tasks([{"id": "T1", "requirements": ["R1"]}])


def test_load_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_golden_tasks(Path("does-not-exist.yaml"))


def test_golden_task_is_constructible_directly() -> None:
    task = GoldenTask(
        id="T1",
        objective="x",
        kind="bug",
        requirements=[GoldenRequirement(id="R1")],
        expected_status="done",
        expected_checks=["lint"],
    )
    assert task.requirement_ids == ["R1"]
