"""Self-Eval Gate (F41) minted-case sandbox metadata.

A minted Self-Eval Gate case is an ``agent_task`` :class:`~forge_eval.golden.GoldenCase`
auto-derived from one of the org's own merged PRs: the issue becomes the query,
and the regression check becomes a set of *hidden* fail-to-pass / pass-to-pass
tests replayed inside a sandbox — never surfaced to the model's context. Rather
than widen the shared, format-agnostic ``GoldenCase`` dataclass (used by every
other golden-set kind too), these sandbox fields are carried in
``GoldenCase.metadata`` and validated here.

:func:`validate_freezable` (``forge_eval.benchmark.manifest``) already rejects
any ``agent_task`` case declaring ``expected_terminal_state == "merged"``; a
minted case must terminate no later than ``pr_opened``/``awaiting_review`` —
the harness never merges on the model's behalf (AC24, human approval gate).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from forge_eval.golden import GoldenCase

__all__ = ["SweCaseFields", "parse_swe_case_fields"]


class SweCaseFields(BaseModel):
    """Typed view of a minted Self-Eval Gate case's sandbox wiring.

    All fields are optional at the type level (a plain retrieval/task case has
    none of them); a minted SWE case is expected to set every field.
    """

    #: Test node ids that must go fail -> pass after a correct resolution.
    fail_to_pass: list[str] = Field(default_factory=list)
    #: Test node ids that must stay passing (regression guard).
    pass_to_pass: list[str] = Field(default_factory=list)
    #: Sandbox base image/tag the case replays against.
    sandbox_image: str | None = None
    #: Ordered shell commands run once to prepare the sandbox before scoring.
    setup_commands: list[str] = Field(default_factory=list)
    #: Commit the sandbox checks out before applying any candidate change.
    base_commit: str | None = None


def parse_swe_case_fields(case: GoldenCase) -> SweCaseFields:
    """Extract + validate the Self-Eval Gate sandbox fields from ``case.metadata``.

    The hidden test ids never enter a model prompt: only the sandbox harness
    reads ``fail_to_pass``/``pass_to_pass`` off the parsed result.
    """
    return SweCaseFields.model_validate(case.metadata)
