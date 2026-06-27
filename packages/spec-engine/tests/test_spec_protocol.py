"""Protocol-conformance + determinism tests for ``forge_spec``.

The engine must structurally satisfy the FROZEN ``SpecEngine`` Protocol
(forge_contracts) so the API/worker layer can depend on the interface, and its
id derivation must be deterministic so a re-instantiated engine resolves the
same specs/tasks from disk.
"""

from __future__ import annotations

import inspect
import uuid

from forge_contracts import SpecEngine
from forge_spec import FileSpecEngine, spec_id_for_key, task_id_for


def test_file_spec_engine_satisfies_spec_engine_protocol(tmp_path) -> None:
    engine = FileSpecEngine(tmp_path)
    assert isinstance(engine, SpecEngine)


def test_engine_exposes_every_protocol_method(tmp_path) -> None:
    engine = FileSpecEngine(tmp_path)
    for name in (
        "constitution_init",
        "spec_create",
        "spec_clarify",
        "spec_plan",
        "spec_tasks",
        "validate",
        "read_manifest",
        "write_manifest",
    ):
        assert callable(getattr(engine, name)), f"missing protocol method {name}"


def test_protocol_method_signatures_match_contract(tmp_path) -> None:
    # The impl must accept the exact frozen parameter names.
    engine = FileSpecEngine(tmp_path)
    sig = inspect.signature(engine.spec_create)
    assert list(sig.parameters) == ["epic_id", "name", "requirements"]

    sig_const = inspect.signature(engine.constitution_init)
    assert list(sig_const.parameters) == ["project_id", "principles"]


def test_task_id_for_is_deterministic(tmp_path) -> None:
    a = task_id_for("SPEC-1", "SPEC-1-T1")
    b = task_id_for("SPEC-1", "SPEC-1-T1")
    c = task_id_for("SPEC-1", "SPEC-1-T2")
    assert isinstance(a, uuid.UUID)
    assert a == b != c


def test_generated_task_ids_are_stable_across_engines(tmp_path) -> None:
    from forge_contracts import Requirement

    eng1 = FileSpecEngine(tmp_path)
    manifest = eng1.spec_create(uuid.uuid4(), "Stable", [Requirement(id="R1", text="do a thing")])
    spec_id = spec_id_for_key(manifest.id)
    eng1.approve_spec(spec_id)
    ids_1 = [t.id for t in eng1.spec_tasks(spec_id)]

    eng2 = FileSpecEngine(tmp_path)
    ids_2 = [t.id for t in eng2.spec_tasks(spec_id)]
    assert ids_1 == ids_2
