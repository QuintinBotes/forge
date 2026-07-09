"""ss-draft: BYOK AI spec drafting (``POST /spec/draft``).

Covers the service (prompt shape seeded with the constitution, streaming
assembly, parse to a manifest preview, token/cost accounting) and the wired
endpoint (draft-only, RBAC, constitution seeding). The ``ModelClient`` is
MOCKED throughout — no live key, no network.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_agent.providers import cost_usd
from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.routers.spec import DraftModelBinding, get_draft_binding, get_spec_engine
from forge_api.services.spec_draft_service import (
    DRAFT_PLACEHOLDER_ID,
    build_system_prompt,
    draft_spec,
    estimate_tokens,
)
from forge_contracts import (
    AcceptanceCriterion,
    Constitution,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Requirement,
    SpecManifest,
    TokenUsage,
)
from forge_contracts.enums import SpecStatus, UserRole
from forge_spec import FileSpecEngine, render_spec_md, spec_id_for_key

_MODEL = "claude-opus-4-8"


def _principal(role: UserRole = UserRole.ADMIN) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role=role,
        email="test@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _sample_spec_md() -> str:
    """A well-formed spec.md the model would emit (guaranteed round-trippable)."""
    manifest = SpecManifest(
        id=DRAFT_PLACEHOLDER_ID,
        name="Customer search by name",
        status=SpecStatus.DRAFT,
        requirements=[Requirement(id="R1", text="Search customers by name")],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="A1",
                text="Given a name, when searching, then matching customers are returned",
                req_refs=["R1"],
            )
        ],
    )
    return render_spec_md(manifest)


class _StreamingSpy:
    """A mocked ``ModelClient`` that streams ``chunks`` and records requests."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover - unused
        self.requests.append(request)
        return ModelResponse(content="".join(self._chunks))

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        self.requests.append(request)
        for chunk in self._chunks:
            yield ModelStreamEvent(type="text", text=chunk, delta=chunk)


def _chunked(text: str, size: int = 37) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


# --------------------------------------------------------------------------- #
# Service unit tests                                                           #
# --------------------------------------------------------------------------- #


def test_estimate_tokens_is_deterministic_and_nonzero() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") == 1
    assert estimate_tokens("abcd" * 10) == 10


def test_build_system_prompt_seeds_constitution() -> None:
    constitution = Constitution(
        project_id=uuid.uuid4(),
        principles=["Prefer boring technology", "Tests are non-negotiable"],
        architecture_guardrails=["Singular table names", "tz-aware datetimes"],
    )
    prompt = build_system_prompt(constitution)
    assert "Prefer boring technology" in prompt
    assert "Tests are non-negotiable" in prompt
    assert "Singular table names" in prompt
    # The spec.md format contract is always present so the draft parses.
    assert "## Goal" in prompt
    assert DRAFT_PLACEHOLDER_ID in prompt


def test_build_system_prompt_without_constitution() -> None:
    prompt = build_system_prompt(None)
    assert "## Goal" in prompt  # the spec.md format contract is always present
    assert DRAFT_PLACEHOLDER_ID in prompt
    assert "constitution" not in prompt.lower()  # none supplied -> not injected


def test_draft_spec_assembles_stream_and_parses() -> None:
    spec_md = _sample_spec_md()
    client = _StreamingSpy(_chunked(spec_md))

    draft = draft_spec(client, goal="Let users search customers by name", model=_MODEL)

    # Streaming assembly reconstructed the full document across chunks.
    assert draft.spec_md == spec_md
    assert draft.parse_error is None
    assert draft.manifest is not None
    assert draft.manifest.name == "Customer search by name"
    assert draft.manifest.requirements[0].id == "R1"
    assert draft.model == _MODEL


def test_draft_spec_prompt_shape_carries_goal_and_constitution() -> None:
    client = _StreamingSpy(_chunked(_sample_spec_md()))
    constitution = Constitution(project_id=uuid.uuid4(), principles=["Ship small, safe changes"])
    epic_id = uuid.uuid4()

    draft_spec(
        client,
        goal="Add SSO login",
        model=_MODEL,
        constitution=constitution,
        epic_id=epic_id,
    )

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.model == _MODEL
    assert request.system is not None and "Ship small, safe changes" in request.system
    user = request.messages[-1].content
    assert "Add SSO login" in user
    assert str(epic_id) in user


def test_draft_spec_records_cost_via_pricing_table() -> None:
    spec_md = _sample_spec_md()
    client = _StreamingSpy(_chunked(spec_md))

    draft = draft_spec(client, goal="Search customers", model=_MODEL)

    usage = draft.usage
    assert usage["output_tokens"] == estimate_tokens(spec_md)
    assert usage["input_tokens"] > 0
    assert usage["calls"] == 1
    # Cost rides the shared HARD-02 pricing table (not reimplemented here).
    expected = cost_usd(
        _MODEL,
        TokenUsage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]),
    )
    assert usage["cost_usd"] == expected
    assert usage["cost_usd"] > 0.0


def test_draft_spec_parse_error_is_graceful() -> None:
    client = _StreamingSpy(["This is not a spec at all, just prose."])

    draft = draft_spec(client, goal="whatever", model=_MODEL)

    assert draft.manifest is None
    assert draft.parse_error is not None
    assert draft.spec_md  # raw text still returned for the human to fix
    assert draft.usage["cost_usd"] >= 0.0


def test_draft_spec_strips_code_fence_wrapper() -> None:
    spec_md = _sample_spec_md()
    fenced = "```markdown\n" + spec_md + "```\n"
    client = _StreamingSpy(_chunked(fenced))

    draft = draft_spec(client, goal="Search customers", model=_MODEL)

    assert draft.manifest is not None
    assert draft.manifest.name == "Customer search by name"


# --------------------------------------------------------------------------- #
# Endpoint integration tests                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def spy() -> _StreamingSpy:
    return _StreamingSpy(_chunked(_sample_spec_md()))


def _make_client(
    tmp_path: Path,
    authenticate_app: Callable[..., FastAPI],
    spy: _StreamingSpy,
    *,
    role: UserRole = UserRole.ADMIN,
) -> tuple[TestClient, FileSpecEngine]:
    app = create_app()
    authenticate_app(app, _principal(role=role))
    engine = FileSpecEngine(root=tmp_path / "specs")
    app.dependency_overrides[get_spec_engine] = lambda: engine
    app.dependency_overrides[get_draft_binding] = lambda: DraftModelBinding(
        client=spy, model=_MODEL
    )
    return TestClient(app), engine


def test_draft_endpoint_returns_manifest_preview(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI], spy: _StreamingSpy
) -> None:
    client, _ = _make_client(tmp_path, authenticate_app, spy)
    with client:
        resp = client.post("/spec/draft", json={"goal": "Search customers by name"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == _MODEL
    assert body["manifest"]["name"] == "Customer search by name"
    assert body["parse_error"] is None
    assert body["usage"]["cost_usd"] > 0.0
    assert body["spec_md"].startswith("---")


def test_draft_endpoint_seeds_constitution_from_project(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI], spy: _StreamingSpy
) -> None:
    client, engine = _make_client(tmp_path, authenticate_app, spy)
    project_id = uuid.uuid4()
    engine.constitution_init(project_id, ["Latency budget is 200ms"])
    with client:
        resp = client.post(
            "/spec/draft",
            json={"goal": "Add a caching layer", "project_id": str(project_id)},
        )
    assert resp.status_code == 200, resp.text
    # The seeded constitution principle reached the model's system prompt.
    assert spy.requests
    assert "Latency budget is 200ms" in (spy.requests[0].system or "")


def test_draft_endpoint_requires_write_permission(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI], spy: _StreamingSpy
) -> None:
    client, _ = _make_client(tmp_path, authenticate_app, spy, role=UserRole.VIEWER)
    with client:
        resp = client.post("/spec/draft", json={"goal": "Search customers"})
    assert resp.status_code == 403


def test_draft_endpoint_rejects_empty_goal(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI], spy: _StreamingSpy
) -> None:
    client, _ = _make_client(tmp_path, authenticate_app, spy)
    with client:
        resp = client.post("/spec/draft", json={"goal": ""})
    assert resp.status_code == 422


def test_draft_endpoint_does_not_persist(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI], spy: _StreamingSpy
) -> None:
    """Draft-only: the previewed spec is not written to the engine."""
    client, _ = _make_client(tmp_path, authenticate_app, spy)
    with client:
        resp = client.post("/spec/draft", json={"goal": "Search customers by name"})
        assert resp.status_code == 200
        spec_uuid = spec_id_for_key(DRAFT_PLACEHOLDER_ID)
        fetched = client.get(f"/spec/specs/{spec_uuid}")
    assert fetched.status_code == 404
