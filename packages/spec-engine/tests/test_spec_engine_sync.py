"""Dual-format engine tests for ``forge_spec`` (slice ss-engine).

``FileSpecEngine`` treats ``spec.md`` (prose) and ``manifest.yaml`` (machine) as
BOTH first-class, EDITABLE serializations of the one canonical
:class:`SpecManifest`. These tests pin the engine-level contract on top of the
already-tested parser/serializer:

- a spec can be CREATED and EDITED from EITHER format;
- every write keeps the two files in sync (they always parse to the same
  manifest);
- manifest-only (legacy) and md-only specs still load (back-compat);
- if the two diverge out-of-band the engine reconciles by last-write-wins
  (mtime) and warns.
"""

from __future__ import annotations

import os
import uuid

import pytest

from forge_contracts import (
    AcceptanceCriterion,
    Requirement,
    SpecManifest,
    SpecStatus,
)
from forge_spec import (
    FileSpecEngine,
    SpecReconcileWarning,
    dump_manifest,
    load_manifest,
    parse_spec_md,
    render_spec_md,
    spec_id_for_key,
)
from forge_spec import manifest as manifest_io


@pytest.fixture
def engine(tmp_path) -> FileSpecEngine:
    return FileSpecEngine(tmp_path)


def _manifest(spec_id: str = "SPEC-1", name: str = "Customer endpoint") -> SpecManifest:
    return SpecManifest(
        id=spec_id,
        name=name,
        status=SpecStatus.DRAFT,
        requirements=[
            Requirement(id="R1", text="Add customer search endpoint"),
            Requirement(id="R2", text="Support bearer auth"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(id="A1", req_refs=["R1"], text="cursor + limit params"),
        ],
        constraints=["No breaking changes before v2"],
    )


def _both_files_agree(engine: FileSpecEngine, spec_id: uuid.UUID) -> SpecManifest:
    """Assert spec.md and manifest.yaml on disk parse to the SAME manifest; return it."""
    spec_dir = engine.spec_path(spec_id)
    md_text = (spec_dir / manifest_io.SPEC_FILENAME).read_text(encoding="utf-8")
    yaml_text = (spec_dir / manifest_io.MANIFEST_FILENAME).read_text(encoding="utf-8")
    from_md = parse_spec_md(md_text)
    from_yaml = load_manifest(yaml_text)
    assert from_md == from_yaml
    return from_md


# --------------------------------------------------------------------------- #
# Create / edit via spec.md                                                    #
# --------------------------------------------------------------------------- #


def test_create_via_spec_md_writes_both_formats(engine) -> None:
    manifest = _manifest("SPEC-1", "Created from md")
    saved = engine.save_spec_md(render_spec_md(manifest))

    assert saved == manifest
    spec_id = spec_id_for_key(manifest.id)
    spec_dir = engine.spec_path(spec_id)
    assert (spec_dir / "spec.md").exists()
    assert (spec_dir / "manifest.yaml").exists()
    assert _both_files_agree(engine, spec_id) == manifest


def test_edit_via_spec_md_updates_manifest_yaml(engine) -> None:
    manifest = _manifest("SPEC-1")
    engine.save_spec_md(render_spec_md(manifest))
    spec_id = spec_id_for_key(manifest.id)

    edited = manifest.model_copy(update={"constraints": ["Edited: P99 < 200ms", "and via md"]})
    engine.save_spec_md(render_spec_md(edited))

    reloaded = engine.read_manifest(spec_id)
    assert reloaded.constraints == ["Edited: P99 < 200ms", "and via md"]
    # The machine format was re-rendered to match the prose edit.
    assert _both_files_agree(engine, spec_id) == edited


# --------------------------------------------------------------------------- #
# Create / edit via manifest.yaml                                              #
# --------------------------------------------------------------------------- #


def test_create_via_manifest_yaml_writes_both_formats(engine) -> None:
    manifest = _manifest("SPEC-1", "Created from yaml")
    saved = engine.save_manifest_yaml(dump_manifest(manifest))

    assert saved == manifest
    spec_id = spec_id_for_key(manifest.id)
    spec_dir = engine.spec_path(spec_id)
    assert (spec_dir / "spec.md").exists()
    assert (spec_dir / "manifest.yaml").exists()
    assert _both_files_agree(engine, spec_id) == manifest


def test_edit_via_manifest_yaml_updates_spec_md(engine) -> None:
    manifest = _manifest("SPEC-1")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)

    edited = manifest.model_copy(
        update={"requirements": [Requirement(id="R1", text="Edited requirement via yaml")]}
    )
    engine.save_manifest_yaml(dump_manifest(edited))

    # spec.md was re-rendered from the yaml edit.
    md_text = engine.read_spec_md(spec_id)
    assert "Edited requirement via yaml" in md_text
    assert parse_spec_md(md_text).requirements[0].text == "Edited requirement via yaml"
    assert _both_files_agree(engine, spec_id) == edited


# --------------------------------------------------------------------------- #
# md <-> yaml stay consistent across the whole lifecycle                        #
# --------------------------------------------------------------------------- #


def test_lifecycle_keeps_both_formats_in_sync(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Sync me", _manifest().requirements)
    spec_id = spec_id_for_key(manifest.id)

    _both_files_agree(engine, spec_id)
    engine.spec_clarify(spec_id)
    _both_files_agree(engine, spec_id)
    engine.spec_plan(spec_id)
    _both_files_agree(engine, spec_id)
    engine.approve_spec(spec_id)
    # After every lifecycle write the prose and machine formats still agree.
    assert _both_files_agree(engine, spec_id) == engine.read_manifest(spec_id)


def test_read_spec_md_matches_read_manifest(engine) -> None:
    manifest = _manifest("SPEC-1")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)
    assert parse_spec_md(engine.read_spec_md(spec_id)) == engine.read_manifest(spec_id)


# --------------------------------------------------------------------------- #
# Back-compat: manifest-only and md-only specs                                 #
# --------------------------------------------------------------------------- #


def test_back_compat_manifest_only_spec_loads(engine) -> None:
    manifest = _manifest("SPEC-1", "Legacy manifest-only")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)
    # Simulate a legacy spec that predates dual-format: only manifest.yaml.
    (engine.spec_path(spec_id) / manifest_io.SPEC_FILENAME).unlink()

    reloaded = FileSpecEngine(engine.root).read_manifest(spec_id)
    assert reloaded == manifest
    # Rendering its prose surface still works (derived from the manifest).
    assert parse_spec_md(FileSpecEngine(engine.root).read_spec_md(spec_id)) == manifest


def test_back_compat_md_only_spec_loads(engine) -> None:
    manifest = _manifest("SPEC-1", "md-only spec")
    engine.save_spec_md(render_spec_md(manifest))
    spec_id = spec_id_for_key(manifest.id)
    # Only spec.md on disk (authored purely as prose, no machine sidecar yet).
    (engine.spec_path(spec_id) / manifest_io.MANIFEST_FILENAME).unlink()

    reloaded = FileSpecEngine(engine.root).read_manifest(spec_id)
    assert reloaded == manifest


def test_saving_md_only_spec_regenerates_manifest_yaml(engine) -> None:
    manifest = _manifest("SPEC-1", "md-only spec")
    engine.save_spec_md(render_spec_md(manifest))
    spec_id = spec_id_for_key(manifest.id)
    (engine.spec_path(spec_id) / manifest_io.MANIFEST_FILENAME).unlink()

    fresh = FileSpecEngine(engine.root)
    fresh.reconcile(spec_id)
    assert (engine.spec_path(spec_id) / manifest_io.MANIFEST_FILENAME).exists()
    assert _both_files_agree(fresh, spec_id) == manifest


# --------------------------------------------------------------------------- #
# Out-of-band divergence: last-write-wins + warning                            #
# --------------------------------------------------------------------------- #


def _diverge(engine: FileSpecEngine, spec_id: uuid.UUID, *, md_wins: bool) -> SpecManifest:
    """Rewrite spec.md with a different manifest and set mtimes so md/yaml wins."""
    spec_dir = engine.spec_path(spec_id)
    md_path = spec_dir / manifest_io.SPEC_FILENAME
    yaml_path = spec_dir / manifest_io.MANIFEST_FILENAME
    base = engine.read_manifest(spec_id)
    hand_edited = base.model_copy(update={"name": "Edited by hand out of band"})
    md_path.write_text(render_spec_md(hand_edited), encoding="utf-8")
    # Make the winner's mtime strictly newer.
    if md_wins:
        os.utime(yaml_path, (1_000_000, 1_000_000))
        os.utime(md_path, (2_000_000, 2_000_000))
    else:
        os.utime(md_path, (1_000_000, 1_000_000))
        os.utime(yaml_path, (2_000_000, 2_000_000))
    return hand_edited


def test_divergence_last_write_wins_md_newer(engine) -> None:
    manifest = _manifest("SPEC-1", "Original")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)
    hand_edited = _diverge(engine, spec_id, md_wins=True)

    with pytest.warns(SpecReconcileWarning):
        resolved = FileSpecEngine(engine.root).read_manifest(spec_id)
    assert resolved.name == hand_edited.name == "Edited by hand out of band"


def test_divergence_last_write_wins_yaml_newer(engine) -> None:
    manifest = _manifest("SPEC-1", "Original")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)
    _diverge(engine, spec_id, md_wins=False)

    with pytest.warns(SpecReconcileWarning):
        resolved = FileSpecEngine(engine.root).read_manifest(spec_id)
    # manifest.yaml is newer -> it wins, the hand md edit is discarded.
    assert resolved.name == "Original"


def test_reconcile_rewrites_both_in_sync(engine) -> None:
    manifest = _manifest("SPEC-1", "Original")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)
    _diverge(engine, spec_id, md_wins=True)

    fresh = FileSpecEngine(engine.root)
    with pytest.warns(SpecReconcileWarning):
        winner = fresh.reconcile(spec_id)

    assert winner.name == "Edited by hand out of band"
    # After reconcile the two files agree again -> no further warning.
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        assert _both_files_agree(fresh, spec_id) == winner


def test_in_sync_files_do_not_warn(engine) -> None:
    manifest = _manifest("SPEC-1")
    engine.save_manifest_yaml(dump_manifest(manifest))
    spec_id = spec_id_for_key(manifest.id)

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        # A normal read of an in-sync spec must never trigger a reconcile warning.
        FileSpecEngine(engine.root).read_manifest(spec_id)
