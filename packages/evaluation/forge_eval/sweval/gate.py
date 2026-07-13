"""The Self-Eval Gate: block a config change that regresses on the private suite.

A model/prompt/router config change is refused if, evaluated against the
workspace's private per-repo suite, its resolution rate drops below the recorded
baseline. The eval runner and baseline lookup are injected (production wires the
agent runtime + sandbox + a persisted baseline; tests inject a fake), and the
gate is a no-op on cold start (no baseline / no private suite) so existing config
flows stay green until a suite exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from forge_eval.sweval.self_eval import SelfEvalScorecard

#: Run the private-suite Self-Eval for a proposed config; None = no private suite.
EvalRunner = Callable[[UUID, Any], Awaitable[SelfEvalScorecard | None]]
#: Recorded baseline resolution rate for a workspace; None = no baseline yet.
BaselineLookup = Callable[[UUID], float | None]


class SelfEvalRegressionError(Exception):
    """Raised when a proposed config regresses the private-suite resolution rate."""

    def __init__(self, *, scorecard: SelfEvalScorecard, baseline_rate: float) -> None:
        self.scorecard = scorecard
        self.baseline_rate = baseline_rate
        super().__init__(
            f"Self-Eval regression: resolution rate "
            f"{scorecard.resolution_rate:.3f} < baseline {baseline_rate:.3f} "
            f"({scorecard.resolved}/{scorecard.total} cases resolved)"
        )


@dataclass(frozen=True)
class SelfEvalGate:
    """Gate a config change on the private-suite resolution rate."""

    eval_runner: EvalRunner
    baseline_for: BaselineLookup

    async def check_config(
        self, workspace_id: UUID, proposed_config: Any, *, force: bool = False
    ) -> SelfEvalScorecard | None:
        """Raise :class:`SelfEvalRegressionError` if ``proposed_config`` regresses.

        Returns the passing scorecard (or None on cold start / forced override).
        """
        if force:
            return None
        baseline = self.baseline_for(workspace_id)
        if baseline is None:
            return None  # cold start — no baseline to regress against
        scorecard = await self.eval_runner(workspace_id, proposed_config)
        if scorecard is None:
            return None  # no private suite for this workspace
        if not scorecard.meets(baseline):
            raise SelfEvalRegressionError(scorecard=scorecard, baseline_rate=baseline)
        return scorecard
