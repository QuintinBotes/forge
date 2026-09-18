"""Task generation reads the spec rather than mechanically wrapping it (#113).

Generation used to be a 1:1 map over `requirements`, so three things went wrong
at once: a requirement phrased as a scope *exclusion* became a task marked
`ready_for_agent` telling an agent to do the thing the spec ruled out; the task
title was the requirement's full text, so a board row was a paragraph; and the
task carried no link back to the epic the spec was created for.
"""

from __future__ import annotations

import uuid

import pytest

from forge_contracts import Requirement, TaskStatus
from forge_spec import FileSpecEngine, spec_id_for_key


@pytest.fixture
def engine(tmp_path) -> FileSpecEngine:
    return FileSpecEngine(tmp_path)


def _approved(engine: FileSpecEngine, epic_id: uuid.UUID, **kw):
    manifest = engine.spec_create(
        epic_id,
        "Dedupe onboarding records",
        [Requirement(id="R1", text="Reject a duplicate submission at the API boundary.")],
    )
    for key, value in kw.items():
        setattr(manifest, key, value)
    engine.write_manifest(manifest)
    spec_id = spec_id_for_key(manifest.id)
    engine.approve_spec(spec_id)
    return spec_id


# --- non-goals never become work ------------------------------------------- #


def test_a_non_goal_does_not_become_a_task(engine: FileSpecEngine) -> None:
    """The reported case: an exclusion came back as `ready_for_agent`."""
    spec_id = _approved(
        engine,
        uuid.uuid4(),
        non_goals=[
            Requirement(
                id="N1",
                text=(
                    "The unique index and the production duplicate measurement stay out "
                    "of scope and remain on MOD-811 as separate work."
                ),
            )
        ],
    )

    tasks = engine.spec_tasks(spec_id)

    assert len(tasks) == 1
    assert all("out of scope" not in (t.description or "") for t in tasks)
    assert all("unique index" not in t.title for t in tasks)


def test_non_goals_survive_a_round_trip(engine: FileSpecEngine) -> None:
    """They are only useful if both serializations carry them."""
    spec_id = _approved(
        engine, uuid.uuid4(), non_goals=[Requirement(id="N1", text="No schema migration.")]
    )

    reread = engine.read_manifest(spec_id)

    assert [(n.id, n.text) for n in reread.non_goals] == [("N1", "No schema migration.")]


# --- a title is a title ----------------------------------------------------- #


def test_a_paragraph_requirement_yields_a_one_line_title(engine: FileSpecEngine) -> None:
    long_text = (
        "Each endpoint group on the onboarding-dashboard service requires a named "
        "authorization policy that is satisfied by an Entra app role on the caller's "
        "token, not by a caller-id allow list."
    )
    manifest = engine.spec_create(
        uuid.uuid4(), "Gate endpoints", [Requirement(id="R1", text=long_text)]
    )
    spec_id = spec_id_for_key(manifest.id)
    engine.approve_spec(spec_id)

    task = engine.spec_tasks(spec_id)[0]

    assert len(task.title) <= 80
    assert "\n" not in task.title
    # The full text is not lost — it is the description.
    assert long_text in (task.description or "")


# --- generated tasks link back to the board -------------------------------- #


def test_a_generated_task_carries_the_epic(engine: FileSpecEngine) -> None:
    """`spec_create` took an epic_id and discarded it, orphaning every task."""
    epic_id = uuid.uuid4()
    spec_id = _approved(engine, epic_id)

    task = engine.spec_tasks(spec_id)[0]

    assert task.epic_id == epic_id
    assert task.status is TaskStatus.READY_FOR_AGENT


def test_the_epic_survives_a_round_trip(engine: FileSpecEngine) -> None:
    epic_id = uuid.uuid4()
    spec_id = _approved(engine, epic_id)

    assert engine.read_manifest(spec_id).epic_id == epic_id
