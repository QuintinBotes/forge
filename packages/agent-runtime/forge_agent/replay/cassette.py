"""The :class:`RunCassette` and its digest helpers.

A cassette is the on-tape record of a single agent run: an ordered list of
:class:`RecordedLLMCall` (one per ``ModelClient.complete``) and an ordered list
of :class:`RecordedToolCall` (one per ``ToolRegistry.dispatch``), plus a redacted
snapshot of the environment/config the run executed under.

Determinism note: the target providers 400 on ``seed``/``temperature`` (see
``providers/translate.py``), so replay is by **substitution** — the recorded
``response``/``result`` is returned by call-index. The ``request_digest`` /
``args_digest`` on each entry are the correctness net: on replay a wrapper
compares the incoming digest against the recorded one at that index and raises on
divergence (mirroring Temporal's replay-divergence canary).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from forge_agent.tools import ToolResult
from forge_contracts import ModelRequest, ModelResponse
from forge_contracts.auth import SecretRedactor

__all__ = [
    "RecordedLLMCall",
    "RecordedToolCall",
    "RunCassette",
    "args_digest",
    "canonical_json",
    "request_digest",
]


def _jsonable(value: Any) -> Any:
    """Coerce pydantic models / dataclasses / mappings into JSON-native data.

    This keeps the canonical serialisation stable regardless of whether a value
    is a ``forge_contracts`` DTO (pydantic) or a runtime dataclass like
    :class:`~forge_agent.tools.ToolResult`.
    """
    # Pydantic models (ModelRequest/ModelResponse and friends).
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """A deterministic JSON encoding: sorted keys, no insignificant whitespace."""
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(request: ModelRequest) -> str:
    """Stable content digest of a model request (canonical-json sha256)."""
    return _sha256(canonical_json(request))


def args_digest(arguments: Mapping[str, Any]) -> str:
    """Stable content digest of a tool's arguments (canonical-json sha256)."""
    return _sha256(canonical_json(dict(arguments)))


@dataclass(frozen=True)
class RecordedLLMCall:
    """One recorded ``ModelClient.complete`` call and its response."""

    index: int
    request_digest: str
    response: ModelResponse
    model: str
    ts: float


@dataclass(frozen=True)
class RecordedToolCall:
    """One recorded ``ToolRegistry.dispatch`` call and its result."""

    index: int
    name: str
    args_digest: str
    result: ToolResult
    ts: float


@dataclass
class RunCassette:
    """The ordered record of a run's LLM and tool calls plus an env snapshot.

    LLM and tool calls are indexed independently (each boundary is replayed by
    its own call-index). ``env`` is a redacted snapshot of the config/environment
    the recording ran under.
    """

    llm_calls: list[RecordedLLMCall] = field(default_factory=list)
    tool_calls: list[RecordedToolCall] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    #: Clock used for entry timestamps; injectable so tests stay deterministic.
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    @classmethod
    def with_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        redactor: SecretRedactor | None = None,
        clock: Callable[[], float] = time.time,
    ) -> RunCassette:
        """Build a cassette with a *redacted* env/config snapshot.

        Every value is passed through ``redactor.redact`` when a redactor is
        supplied so secrets never land on the tape.
        """
        snapshot: dict[str, str] = {}
        for key, value in (env or {}).items():
            text = str(value)
            snapshot[str(key)] = redactor.redact(text) if redactor is not None else text
        return cls(env=snapshot, clock=clock)

    def record_llm(self, request: ModelRequest, response: ModelResponse) -> RecordedLLMCall:
        """Append an LLM call+response and return the recorded entry."""
        entry = RecordedLLMCall(
            index=len(self.llm_calls),
            request_digest=request_digest(request),
            response=response,
            model=response.model or request.model,
            ts=self.clock(),
        )
        self.llm_calls.append(entry)
        return entry

    def record_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> RecordedToolCall:
        """Append a tool call+result and return the recorded entry."""
        entry = RecordedToolCall(
            index=len(self.tool_calls),
            name=name,
            args_digest=args_digest(arguments),
            result=result,
            ts=self.clock(),
        )
        self.tool_calls.append(entry)
        return entry

    def to_dict(self) -> dict[str, Any]:
        """A JSON-native snapshot of the cassette (for persistence/artifacts)."""
        return {
            "llm_calls": [
                {
                    "index": c.index,
                    "request_digest": c.request_digest,
                    "response": _jsonable(c.response),
                    "model": c.model,
                    "ts": c.ts,
                }
                for c in self.llm_calls
            ],
            "tool_calls": [
                {
                    "index": c.index,
                    "name": c.name,
                    "args_digest": c.args_digest,
                    "result": _jsonable(c.result),
                    "ts": c.ts,
                }
                for c in self.tool_calls
            ],
            "env": dict(self.env),
        }
