"""The independent reviewer's verdict on a governed run's change (#173)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ReviewFinding", "ReviewVerdict"]


class ReviewFinding(BaseModel):
    """One problem the reviewer found, anchored on a file and line."""

    model_config = ConfigDict(extra="ignore")

    file: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: Literal["blocking", "important", "minor", "nit"]
    statement: str = Field(min_length=1)


class ReviewVerdict(BaseModel):
    """Approve the change, or send it back to the builder with findings.

    ``reason`` names why a verdict was decided without the reviewer, such as
    ``unparseable_verdict``. ``truncated`` says the reviewer saw only part of the
    diff, so the verdict is not a review of the whole change.
    """

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["approve", "request_changes"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    reason: str | None = None
    truncated: bool = False
    reviewer_model: str | None = None
    builder_model: str | None = None
