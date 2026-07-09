"""ss-import: external spec import (``POST /spec/import``).

Covers the service (direct parse, loose-YAML normalization, loose-markdown
normalization, graceful failure) and the wired endpoint (RBAC, draft-only —
nothing persisted). No model client involved — this is parse/normalize only.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.routers.spec import get_spec_engine
from forge_api.services.spec_import_service import (
    IMPORT_PLACEHOLDER_ID,
    detect_format,
    import_spec,
)
from forge_contracts import Requirement, SpecManifest
from forge_contracts.enums import SpecStatus, UserRole
from forge_spec import FileSpecEngine, render_spec_md, spec_id_for_key

# --------------------------------------------------------------------------- #
# Format detection                                                             #
# --------------------------------------------------------------------------- #


def test_detect_format_honors_explicit_hint() -> None:
    assert detect_format("id: x\nname: y\n", "yaml") == "yaml"
    assert detect_format("id: x\nname: y\n", "markdown") == "markdown"


def test_detect_format_sniffs_markdown_from_headings() -> None:
    assert detect_format("# Title\n\nSome body text.") == "markdown"


def test_detect_format_sniffs_yaml_from_mapping() -> None:
    assert detect_format("title: Thing\nrequirements:\n  - do X\n") == "yaml"


def test_detect_format_falls_back_to_markdown_for_unparseable() -> None:
    assert detect_format("not: [valid: yaml: at: all") == "markdown"


# --------------------------------------------------------------------------- #
# Tier 1: direct parse (already-canonical Forge documents)                    #
# --------------------------------------------------------------------------- #


def _canonical_spec_md() -> str:
    manifest = SpecManifest(
        id="SPEC-42",
        name="Existing spec",
        requirements=[Requirement(id="R1", text="Do the thing")],
    )
    return render_spec_md(manifest)


def test_import_spec_md_that_is_already_canonical_passes_through() -> None:
    spec_md = _canonical_spec_md()

    result = import_spec(spec_md)

    assert result.source_format == "markdown"
    assert result.normalized is False
    assert result.parse_error is None
    assert result.manifest is not None
    assert result.manifest.id == "SPEC-42"
    assert result.spec_md == spec_md


def test_import_manifest_yaml_that_is_already_canonical_passes_through() -> None:
    from forge_spec import dump_manifest

    manifest = SpecManifest(id="SPEC-7", name="Canonical yaml spec")
    yaml_text = dump_manifest(manifest)

    result = import_spec(yaml_text, source_format="yaml")

    assert result.source_format == "yaml"
    assert result.normalized is False
    assert result.parse_error is None
    assert result.manifest is not None
    assert result.manifest.id == "SPEC-7"
    assert result.manifest.name == "Canonical yaml spec"


# --------------------------------------------------------------------------- #
# Tier 2: normalize (loose shapes)                                             #
# --------------------------------------------------------------------------- #


def test_import_loose_markdown_normalizes_sections() -> None:
    content = (
        "# Customer search\n\n"
        "## Requirements\n"
        "- Search customers by name\n"
        "- Filter by status\n\n"
        "## Acceptance Criteria\n"
        "- Given a name, when searching, then matches return\n\n"
        "## Constraints\n"
        "- Must respond within 200ms\n\n"
        "## Open Questions\n"
        "- Should archived customers be included?\n"
    )

    result = import_spec(content)

    assert result.source_format == "markdown"
    assert result.normalized is True
    assert result.parse_error is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest.name == "Customer search"
    assert [r.text for r in manifest.requirements] == [
        "Search customers by name",
        "Filter by status",
    ]
    assert manifest.requirements[0].id == "R1"
    assert manifest.acceptance_criteria[0].req_refs == ["R1", "R2"]
    assert manifest.constraints == ["Must respond within 200ms"]
    assert manifest.open_questions[0].id == "Q1"
    # The normalized preview re-renders as valid, round-trippable spec.md.
    assert result.spec_md.startswith("---")
    from forge_spec import parse_spec_md

    assert parse_spec_md(result.spec_md).name == "Customer search"


def test_import_loose_markdown_without_h1_falls_back_to_first_line() -> None:
    content = "Just some free-form notes about a feature.\n\nNo headings at all here."

    result = import_spec(content)

    assert result.manifest is not None
    assert result.manifest.name == "Just some free-form notes about a feature."
    assert result.manifest.id == IMPORT_PLACEHOLDER_ID


def test_import_loose_yaml_normalizes_alternate_keys() -> None:
    content = (
        "title: Customer search\n"
        "requirements:\n"
        "  - Search customers by name\n"
        "  - Filter by status\n"
        "acceptance:\n"
        "  - Given a name, when searching, then matches return\n"
        "constraints:\n"
        "  - Must respond within 200ms\n"
    )

    result = import_spec(content, source_format="yaml")

    assert result.source_format == "yaml"
    assert result.normalized is True
    assert result.parse_error is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest.name == "Customer search"
    assert len(manifest.requirements) == 2
    assert manifest.requirements[0].id == "R1"
    assert manifest.acceptance_criteria[0].req_refs == ["R1", "R2"]
    assert manifest.constraints == ["Must respond within 200ms"]


def test_import_loose_yaml_with_dict_items_extracts_text() -> None:
    content = "name: Thing\nrequirements:\n  - id: CUSTOM-1\n    text: A dict-shaped requirement\n"

    result = import_spec(content, source_format="yaml")

    assert result.manifest is not None
    assert result.manifest.requirements[0].id == "CUSTOM-1"
    assert result.manifest.requirements[0].text == "A dict-shaped requirement"


def test_import_normalized_result_defaults_to_draft_status() -> None:
    result = import_spec("# Some spec\n\n## Requirements\n- A thing\n")
    assert result.manifest is not None
    assert result.manifest.status == SpecStatus.DRAFT


# --------------------------------------------------------------------------- #
# Tier 3: graceful failure                                                     #
# --------------------------------------------------------------------------- #


def test_import_yaml_that_is_not_a_mapping_fails_gracefully() -> None:
    result = import_spec("- just\n- a\n- list\n", source_format="yaml")

    assert result.manifest is None
    assert result.parse_error is not None
    assert result.spec_md  # raw content preserved for the human to fix


def test_import_empty_markdown_still_returns_a_draft() -> None:
    result = import_spec("")

    # Never raises; an empty document just yields an empty-shaped draft.
    assert result.manifest is not None
    assert result.parse_error is None


# --------------------------------------------------------------------------- #
# Endpoint integration tests                                                   #
# --------------------------------------------------------------------------- #


def _client(
    authenticate_app: Callable[..., FastAPI],
    *,
    role: UserRole = UserRole.ADMIN,
    engine: FileSpecEngine | None = None,
) -> TestClient:
    app = create_app()
    principal = Principal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role=role,
        email="test@forge.local",
        auth_method="test",
        scopes=["*"],
    )
    authenticate_app(app, principal)
    if engine is not None:
        app.dependency_overrides[get_spec_engine] = lambda: engine
    return TestClient(app)


def test_import_endpoint_returns_normalized_draft(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    client = _client(authenticate_app)
    with client:
        resp = client.post(
            "/spec/import",
            json={
                "content": "# My feature\n\n## Requirements\n- Do the thing\n",
                "source_format": "markdown",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_format"] == "markdown"
    assert body["normalized"] is True
    assert body["manifest"]["name"] == "My feature"
    assert body["parse_error"] is None


def test_import_endpoint_auto_detects_yaml(authenticate_app: Callable[..., FastAPI]) -> None:
    client = _client(authenticate_app)
    with client:
        resp = client.post(
            "/spec/import",
            json={"content": "title: A yaml spec\nrequirements:\n  - Do X\n"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_format"] == "yaml"
    assert body["manifest"]["name"] == "A yaml spec"


def test_import_endpoint_requires_write_permission(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    client = _client(authenticate_app, role=UserRole.VIEWER)
    with client:
        resp = client.post("/spec/import", json={"content": "# Thing\n"})
    assert resp.status_code == 403


def test_import_endpoint_rejects_empty_content(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    client = _client(authenticate_app)
    with client:
        resp = client.post("/spec/import", json={"content": ""})
    assert resp.status_code == 422


def test_import_endpoint_does_not_persist(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI]
) -> None:
    """Draft-only: nothing importable ends up written to the spec engine."""
    engine = FileSpecEngine(root=tmp_path / "specs")
    client = _client(authenticate_app, engine=engine)
    with client:
        resp = client.post(
            "/spec/import",
            json={"content": "# Thing\n\n## Requirements\n- Do X\n"},
        )
        assert resp.status_code == 200
        spec_uuid = spec_id_for_key(IMPORT_PLACEHOLDER_ID)
        fetched = client.get(f"/spec/specs/{spec_uuid}")
    assert fetched.status_code == 404
