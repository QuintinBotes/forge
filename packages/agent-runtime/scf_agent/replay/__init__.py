"""Deterministic record/replay of agent runs ("Time-Travel Runs").

The two nondeterministic boundaries of :class:`~scf_agent.runtime.AgentRunner`
— the LLM call (:class:`~scf_contracts.ModelClient`) and the tool call
(:class:`~scf_agent.tools.ToolRegistry`) — are both constructor-injected. This
package provides transparent *recording* wrappers around each: they delegate to
the real client/registry and append every call+result to a :class:`RunCassette`
so a run can later be replayed by substitution (return the recorded value by
call-index), never by re-seeding the model.
"""

from __future__ import annotations

from scf_agent.replay.cassette import (
    RecordedLLMCall,
    RecordedToolCall,
    RunCassette,
    args_digest,
    request_digest,
)
from scf_agent.replay.fork import ForkModelClient, ForkToolRegistry
from scf_agent.replay.player import (
    ReplayDivergenceError,
    ReplayModelClient,
    ReplayToolRegistry,
)
from scf_agent.replay.recorder import RecordingModelClient, RecordingToolRegistry

__all__ = [
    "ForkModelClient",
    "ForkToolRegistry",
    "RecordedLLMCall",
    "RecordedToolCall",
    "RecordingModelClient",
    "RecordingToolRegistry",
    "ReplayDivergenceError",
    "ReplayModelClient",
    "ReplayToolRegistry",
    "RunCassette",
    "args_digest",
    "request_digest",
]
