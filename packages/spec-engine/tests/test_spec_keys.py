"""Spec-key identity: client-supplied keys and per-prefix numbering (F6).

Forge used to own spec numbering outright — every key was ``SPEC-<n>`` and the
id regex accepted nothing else — so a team with an existing tracker could not
use their own key as the spec id and had to bury it in ``name``, where nothing
can look it up. These tests pin the widened contract.
"""

from __future__ import annotations

import uuid

import pytest

from forge_contracts import Requirement
from forge_spec import (
    FileSpecEngine,
    SpecKeyError,
    is_spec_key,
    spec_id_for_key,
    spec_number,
    spec_prefix,
)


def _engine(tmp_path) -> FileSpecEngine:
    return FileSpecEngine(tmp_path)


# --- key grammar ----------------------------------------------------------- #


@pytest.mark.parametrize("key", ["SPEC-1", "MOD-893", "PROJ2-17", "A-0"])
def test_accepts_any_uppercase_prefix(key: str) -> None:
    assert is_spec_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "spec-1",  # lowercase prefix
        "SPEC-",  # no ordinal
        "-17",  # no prefix
        "SPEC-1-T1",  # a task key, not a spec key
        "SPEC 1",  # space
        "893",  # bare number
        "",
    ],
)
def test_rejects_malformed_keys(key: str) -> None:
    assert not is_spec_key(key)


def test_prefix_and_number_are_extracted() -> None:
    assert spec_prefix("MOD-893") == "MOD"
    assert spec_number("MOD-893") == 893
    assert spec_prefix("nonsense") is None
    assert spec_number("nonsense") is None


# --- client-supplied keys --------------------------------------------------- #


def test_spec_create_accepts_a_client_supplied_key(tmp_path) -> None:
    engine = _engine(tmp_path)
    manifest = engine.spec_create(
        uuid.uuid4(), "Rate limiting", [Requirement(id="R1", text="limit")], key="MOD-893"
    )
    assert manifest.id == "MOD-893"


def test_a_client_supplied_key_round_trips_through_its_uuid(tmp_path) -> None:
    """The whole point: the spec is readable at the key the caller chose."""
    engine = _engine(tmp_path)
    engine.spec_create(uuid.uuid4(), "Rate limiting", key="MOD-893")

    reread = engine.read_manifest(spec_id_for_key("MOD-893"))
    assert reread.id == "MOD-893"
    assert reread.name == "Rate limiting"


def test_a_malformed_key_is_rejected(tmp_path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(SpecKeyError, match="not a valid spec key"):
        engine.spec_create(uuid.uuid4(), "Bad", key="mod 893")


def test_a_duplicate_key_is_rejected(tmp_path) -> None:
    """Two specs sharing a key would collide on one uuid5 — refuse up front."""
    engine = _engine(tmp_path)
    engine.spec_create(uuid.uuid4(), "First", key="MOD-893")
    with pytest.raises(SpecKeyError, match="already in use"):
        engine.spec_create(uuid.uuid4(), "Second", key="MOD-893")


# --- numbering -------------------------------------------------------------- #


def test_auto_allocation_still_starts_at_spec_1(tmp_path) -> None:
    engine = _engine(tmp_path)
    assert engine.spec_create(uuid.uuid4(), "First").id == "SPEC-1"
    assert engine.spec_create(uuid.uuid4(), "Second").id == "SPEC-2"


def test_an_imported_key_does_not_inflate_forge_numbering(tmp_path) -> None:
    """Numbering is per-prefix: MOD-893 must not push Forge to SPEC-894."""
    engine = _engine(tmp_path)
    engine.spec_create(uuid.uuid4(), "Imported", key="MOD-893")

    assert engine.spec_create(uuid.uuid4(), "Forge-numbered").id == "SPEC-1"


def test_specs_with_different_prefixes_coexist(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.spec_create(uuid.uuid4(), "Imported", key="MOD-893")
    engine.spec_create(uuid.uuid4(), "Forge-numbered")

    assert engine.read_manifest(spec_id_for_key("MOD-893")).name == "Imported"
    assert engine.read_manifest(spec_id_for_key("SPEC-1")).name == "Forge-numbered"


# --- a create never silently becomes an update (issue #110) ---------------- #


def test_an_allocated_key_that_collides_is_refused(tmp_path, monkeypatch) -> None:
    """The allocator reads what is on disk, so it can hand back a used key.

    A store that has lost documents — an unmounted `spec_root`, a restore, a
    hand edit — restarts the ordinal at 1. Writing that key anyway folded the
    new spec into the existing one as another version: silent data loss, with
    the original recoverable only from `spec_version`.
    """
    engine = _engine(tmp_path)
    engine.spec_create(uuid.uuid4(), "First", key="SPEC-1")

    # Force the allocator to propose a key that is already taken.
    monkeypatch.setattr(FileSpecEngine, "_next_spec_number", lambda self, prefix="SPEC": 1)

    with pytest.raises(SpecKeyError, match="already in use"):
        engine.spec_create(uuid.uuid4(), "Second")

    # The original is untouched — not replaced, not versioned over.
    assert engine.read_manifest(spec_id_for_key("SPEC-1")).name == "First"


# --- generated content must not overstate itself (issues #108, #109) ------- #


def test_no_acceptance_criteria_are_manufactured(tmp_path) -> None:
    engine = _engine(tmp_path)
    manifest = engine.spec_create(uuid.uuid4(), "Thing", [Requirement(id="R1", text="do a thing")])
    assert manifest.acceptance_criteria == []


def test_the_placeholder_adr_is_proposed_not_accepted(tmp_path) -> None:
    """`accepted` asserted an architectural decision nobody had taken."""
    engine = _engine(tmp_path)
    manifest = engine.spec_create(uuid.uuid4(), "Thing", [Requirement(id="R1", text="do a thing")])
    planned = engine.spec_plan(spec_id_for_key(manifest.id))

    adr = planned.decisions[0]
    assert adr.status == "proposed"
    assert "TODO" in adr.decision
