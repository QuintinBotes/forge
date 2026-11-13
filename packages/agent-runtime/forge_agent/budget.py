"""Per-run token budgets, enforced before every model call (ADR-0008).

A budget is a hard stop, not a report: once a run has spent its tokens the next
call returns ``stop_reason="budget_exhausted"``, which the runtime treats as a
provider failure and fails the run visibly. Providers that report no usage
(local CLIs) are charged an estimate of four characters per token — unpriced,
never free.
"""

from __future__ import annotations

from collections.abc import Iterator

from forge_agent.providers.base import ModelClientError
from forge_contracts import ModelClient, ModelRequest, ModelResponse, ModelStreamEvent

#: Default per-run cap when the deployment sets none.
DEFAULT_RUN_TOKEN_BUDGET = 200_000

_CHARS_PER_TOKEN = 4


def _estimate(request: ModelRequest, response: ModelResponse) -> int:
    chars = len(request.system or "") + len(response.content)
    chars += sum(len(str(message.content)) for message in request.messages)
    return -(-chars // _CHARS_PER_TOKEN)


class BudgetedModelClient:
    """Wrap a :class:`ModelClient` so a run stops at ``max_tokens``."""

    def __init__(self, inner: ModelClient, *, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError("a run token budget must be positive")
        self._inner = inner
        self.max_tokens = max_tokens
        self.used_tokens = 0
        self.estimated = False

    @property
    def exhausted(self) -> bool:
        return self.used_tokens >= self.max_tokens

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self.exhausted:
            return ModelResponse(
                content=f"run token budget of {self.max_tokens} exhausted",
                stop_reason="budget_exhausted",
            )
        response = self._inner.complete(request)
        if response.usage is not None:
            self.used_tokens += response.usage.input_tokens + response.usage.output_tokens
        else:
            self.used_tokens += _estimate(request, response)
            self.estimated = True
        return response

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        if self.exhausted:
            raise ModelClientError(f"run token budget of {self.max_tokens} exhausted")
        return self._inner.stream(request)


__all__ = ["DEFAULT_RUN_TOKEN_BUDGET", "BudgetedModelClient"]
