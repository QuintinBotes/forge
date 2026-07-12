"""Red-Team Gate adversary harness (``run_red_team``) — hermetic, OFFLINE.

Drives a scripted heterogeneous adversary against a candidate diff and executes
the adversary's authored test through the *real* (previously-unwired) sandbox
``create``/``run`` path via the local ``worktree`` provider. No network, no live
model. Asserts:

* a REAL executed failing test (exit code != 0 in the sandbox) BLOCKS,
* a passing / no-finding attack SURVIVES (never a self-reported pass),
* a structured spec-violation referencing a real ``AcceptanceCriterion`` BLOCKS,
  while a violation referencing a bogus criterion SURVIVES,
* a HOMOGENEOUS adversary (same provider as the coder) is rejected.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from forge_agent.sandbox import LocalSandboxProvider
from forge_contracts import (
    AcceptanceCriterion,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
)
from forge_coordinator.red_team import (
    HomogeneousAdversaryError,
    RedTeamResult,
    run_red_team,
)

# --------------------------------------------------------------------------- #
# A scripted, offline ModelClient (never calls a provider).                     #
# --------------------------------------------------------------------------- #


class ScriptedModel:
    """Returns a canned sequence of ``ModelResponse`` objects, one per call."""

    def __init__(self, model: str, responses: list[ModelResponse]) -> None:
        self.model = model
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            return ModelResponse(content="done", model=self.model)
        return self._responses.pop(0)

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        raise NotImplementedError


def _write_test_then_finish(*, path: str, content: str, test_cmd: str) -> list[ModelResponse]:
    return [
        ModelResponse(
            content="authoring an adversarial test",
            tool_calls=[
                ModelToolCall(
                    id="tc-1",
                    name="write_test",
                    arguments={"path": path, "content": content, "test_cmd": test_cmd},
                )
            ],
        ),
        ModelResponse(content="authored the attack test"),
    ]


def _report_violation(*, criterion_id: str, detail: str) -> list[ModelResponse]:
    return [
        ModelResponse(
            content="found a spec violation",
            tool_calls=[
                ModelToolCall(
                    id="tc-1",
                    name="report_spec_violation",
                    arguments={"criterion_id": criterion_id, "detail": detail},
                )
            ],
        ),
        ModelResponse(content="reported the violation"),
    ]


CODER_MODEL = "claude-opus-4-8"  # anthropic
ADVERSARY_MODEL = "gpt-4.1"  # openai — heterogeneous


@pytest.fixture
def spec() -> object:
    return type(
        "Spec",
        (),
        {
            "acceptance_criteria": [
                AcceptanceCriterion(id="AC-1", text="parses a valid config"),
                AcceptanceCriterion(id="AC-2", text="rejects an unknown key"),
            ]
        },
    )()


# --------------------------------------------------------------------------- #
# Tests                                                                         #
# --------------------------------------------------------------------------- #


async def test_real_failing_test_blocks(tmp_git_repo: Path, spec: object) -> None:
    adversary = ScriptedModel(
        ADVERSARY_MODEL,
        _write_test_then_finish(
            path="test_attack.py",
            content="assert 1 == 2, 'the diff regressed the new behavior'\n",
            test_cmd="python test_attack.py",
        ),
    )
    result = await run_red_team(
        diff="+def parse(cfg): ...",
        spec=spec,
        adversary_client=adversary,
        sandbox_provider=LocalSandboxProvider(),
        coder_model=CODER_MODEL,
        worktree_path=str(tmp_git_repo),
    )
    assert isinstance(result, RedTeamResult)
    assert result.blocked is True
    assert result.kind == "failing_test"
    assert result.failing_test_ref is not None
    assert result.failing_test_ref.path == "test_attack.py"
    assert result.failing_test_ref.exit_code != 0
    # The test file was REALLY authored into the worktree and executed there.
    assert (tmp_git_repo / "test_attack.py").is_file()


async def test_passing_test_survives_never_self_reported(tmp_git_repo: Path, spec: object) -> None:
    # The adversary authors a test but it PASSES in the sandbox (exit 0): a real
    # execution that does not fail is a SURVIVE, never a self-reported block.
    adversary = ScriptedModel(
        ADVERSARY_MODEL,
        _write_test_then_finish(
            path="test_attack.py",
            content="assert 1 == 1\n",
            test_cmd="python test_attack.py",
        ),
    )
    result = await run_red_team(
        diff="+def parse(cfg): ...",
        spec=spec,
        adversary_client=adversary,
        sandbox_provider=LocalSandboxProvider(),
        coder_model=CODER_MODEL,
        worktree_path=str(tmp_git_repo),
    )
    assert result.blocked is False
    assert result.kind == "survived"


async def test_no_finding_survives(tmp_git_repo: Path, spec: object) -> None:
    # The adversary declares defeat immediately (a plain message, no tool call).
    adversary = ScriptedModel(
        ADVERSARY_MODEL,
        [ModelResponse(content="I could not construct a failing test or find a violation.")],
    )
    result = await run_red_team(
        diff="+def parse(cfg): ...",
        spec=spec,
        adversary_client=adversary,
        sandbox_provider=LocalSandboxProvider(),
        coder_model=CODER_MODEL,
        worktree_path=str(tmp_git_repo),
    )
    assert result.blocked is False
    assert result.kind == "survived"


async def test_structured_spec_violation_blocks(tmp_git_repo: Path, spec: object) -> None:
    adversary = ScriptedModel(
        ADVERSARY_MODEL,
        _report_violation(criterion_id="AC-2", detail="unknown keys are silently accepted"),
    )
    result = await run_red_team(
        diff="+def parse(cfg): ...",
        spec=spec,
        adversary_client=adversary,
        sandbox_provider=LocalSandboxProvider(),
        coder_model=CODER_MODEL,
        worktree_path=str(tmp_git_repo),
    )
    assert result.blocked is True
    assert result.kind == "spec_violation"
    assert result.violation is not None
    assert result.violation.criterion_id == "AC-2"


async def test_spec_violation_for_unknown_criterion_survives(
    tmp_git_repo: Path, spec: object
) -> None:
    # A "violation" that references no real acceptance criterion is not a block.
    adversary = ScriptedModel(
        ADVERSARY_MODEL,
        _report_violation(criterion_id="AC-999", detail="made-up criterion"),
    )
    result = await run_red_team(
        diff="+def parse(cfg): ...",
        spec=spec,
        adversary_client=adversary,
        sandbox_provider=LocalSandboxProvider(),
        coder_model=CODER_MODEL,
        worktree_path=str(tmp_git_repo),
    )
    assert result.blocked is False
    assert result.kind == "survived"


async def test_homogeneous_adversary_rejected(tmp_git_repo: Path, spec: object) -> None:
    # Same provider (anthropic) as the coder -> not a real adversary.
    adversary = ScriptedModel(
        "claude-sonnet-5",  # anthropic, like the coder's claude-opus-4-8
        _write_test_then_finish(
            path="test_attack.py",
            content="assert 1 == 2\n",
            test_cmd="python test_attack.py",
        ),
    )
    with pytest.raises(HomogeneousAdversaryError):
        await run_red_team(
            diff="+def parse(cfg): ...",
            spec=spec,
            adversary_client=adversary,
            sandbox_provider=LocalSandboxProvider(),
            coder_model=CODER_MODEL,
            worktree_path=str(tmp_git_repo),
        )
