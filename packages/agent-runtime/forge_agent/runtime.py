"""The single-agent execution runtime (plan -> act -> observe).

``AgentRunner`` implements the frozen :class:`~forge_contracts.AgentRuntime`
protocol: ``run(objective) -> AgentRunResult``. It wires a :class:`StateGraph`
with four nodes — ``plan`` (first model call), ``act`` (policy-gated tool
dispatch), ``observe`` (model reaction to observations), and ``finalize``
(confidence + handoff) — and drives it with an injected
:class:`~forge_contracts.ModelClient` (a fake in tests).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from forge_agent.context import build_system_prompt
from forge_agent.graph import END, CompiledGraph, StateGraph
from forge_agent.policy_gate import ActionPolicyGate, PolicyGate
from forge_agent.sandbox import SandboxError, WorktreeSandbox, load_agents_md
from forge_agent.state import AgentState
from forge_agent.tools import FINISH_TOOL, ToolRegistry
from forge_contracts import (
    AgentObjective,
    AgentRunResult,
    DecisionEffect,
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    RunStatus,
    Step,
    StepKind,
    ToolCall,
)

__all__ = ["AgentRunner"]


class AgentRunner:
    """A LangGraph-style single-agent loop over an injected model client."""

    def __init__(
        self,
        model: ModelClient,
        *,
        tools: ToolRegistry | None = None,
        gate: PolicyGate | None = None,
        repo_root: str | Path | None = None,
        max_iterations: int = 12,
        use_worktree: bool = False,
        escalate_on_denied: bool = True,
    ) -> None:
        self._model = model
        self._tools = tools if tools is not None else ToolRegistry()
        self._gate: PolicyGate = gate if gate is not None else ActionPolicyGate()
        self._repo_root = Path(repo_root) if repo_root is not None else None
        self._max_iterations = max_iterations
        self._use_worktree = use_worktree
        self._escalate_on_denied = escalate_on_denied
        self._graph: CompiledGraph[AgentState] = self._build_graph()

    # ------------------------------------------------------------------ #
    # AgentRuntime protocol                                              #
    # ------------------------------------------------------------------ #
    def run(self, objective: AgentObjective) -> AgentRunResult:
        sandbox: WorktreeSandbox | None = None
        working_root = self._repo_root
        artifacts: dict[str, object] = {}
        try:
            if self._use_worktree and self._repo_root is not None and _wants_worktree(objective):
                sandbox = WorktreeSandbox(
                    self._repo_root, base_branch=_base_branch(objective)
                )
                try:
                    working_root = sandbox.create(_branch_name(objective))
                    artifacts["worktree_path"] = str(working_root)
                except SandboxError as exc:
                    artifacts["worktree_error"] = str(exc)
                    sandbox = None
                    working_root = self._repo_root
            state = self._init_state(objective, working_root, artifacts)
            final = self._graph.invoke(state)
            if sandbox is not None:
                sandbox.cleanup()
                sandbox = None
                artifacts["worktree_cleaned"] = True
            return self._to_result(final)
        finally:
            # Exception path: ensure the worktree is always removed.
            if sandbox is not None:
                sandbox.cleanup()

    # ------------------------------------------------------------------ #
    # Setup                                                              #
    # ------------------------------------------------------------------ #
    def _init_state(
        self,
        objective: AgentObjective,
        working_root: Path | None,
        artifacts: dict[str, object],
    ) -> AgentState:
        agents_md = objective.context.get("agents_md")
        if not isinstance(agents_md, str):
            agents_md = load_agents_md(working_root) if working_root is not None else None
        artifacts["agents_md_loaded"] = agents_md is not None
        system = build_system_prompt(
            objective, agents_md=agents_md, context=objective.context
        )
        user = objective.objective
        if objective.instructions:
            user = f"{user}\n\n{objective.instructions}"
        return AgentState(
            objective=objective,
            system=system,
            messages=[ModelMessage(role="user", content=user)],
            max_iterations=self._max_iterations,
            artifacts=artifacts,
        )

    def _build_graph(self) -> CompiledGraph[AgentState]:
        graph: StateGraph[AgentState] = StateGraph()
        graph.add_node("plan", self._plan_node)
        graph.add_node("act", self._act_node)
        graph.add_node("observe", self._observe_node)
        graph.add_node("finalize", self._finalize_node)
        graph.set_entry_point("plan")
        graph.add_conditional_edges(
            "plan", self._route, {"act": "act", "finalize": "finalize"}
        )
        graph.add_edge("act", "observe")
        graph.add_conditional_edges(
            "observe", self._route, {"act": "act", "finalize": "finalize"}
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    # ------------------------------------------------------------------ #
    # Nodes                                                              #
    # ------------------------------------------------------------------ #
    def _plan_node(self, state: AgentState) -> AgentState:
        return self._call_model(state, StepKind.PLAN)

    def _observe_node(self, state: AgentState) -> AgentState:
        return self._call_model(state, StepKind.MESSAGE)

    def _call_model(self, state: AgentState, step_kind: StepKind) -> AgentState:
        request = ModelRequest(
            model=state.objective.model or "forge-fake-model",
            system=state.system,
            messages=list(state.messages),
            tools=self._tools.schemas(),
        )
        response: ModelResponse = self._model.complete(request)
        # HARD-02: fold per-turn token usage and remember the real model id the
        # provider reported (used to price the run in ``_to_result``).
        state.usage.add(response.usage)
        if response.model:
            state.model_name = response.model
        # HARD-02: a provider safety stop escalates to a human and stops — no
        # blind retry of a flagged prompt.
        if _is_refusal(response.stop_reason):
            self._apply_refusal(state, response.stop_reason)
            return state
        state.last_content = response.content or ""
        state.messages.append(
            ModelMessage(role="assistant", content=response.content or "")
        )
        # Always record the planning turn; for observe turns, only when the model
        # actually said something.
        if step_kind is StepKind.PLAN or response.content:
            thought = response.content or None
            if thought is None and response.tool_calls:
                thought = "planned tool calls: " + ", ".join(
                    tc.name for tc in response.tool_calls
                )
            state.add_step(Step(kind=step_kind, thought=thought))

        pending: list[ModelToolCall] = []
        for call in response.tool_calls:
            if call.name == FINISH_TOOL:
                state.finished = True
                self._apply_finish(state, call.arguments)
            else:
                pending.append(call)
        state.pending = pending
        if not response.tool_calls:
            # Model produced a final message with no tool use.
            state.finished = True
            if state.output is None:
                state.output = response.content or ""
        return state

    def _act_node(self, state: AgentState) -> AgentState:
        for call in state.pending:
            action = call.arguments.get("action") or self._tools.action_for(call.name)
            tool_call = ToolCall(
                tool=call.name,
                action=str(action),
                arguments=dict(call.arguments),
                path=_opt_str(call.arguments.get("path")),
                resource=_opt_str(call.arguments.get("resource")),
            )
            decision = self._gate.evaluate(tool_call, state.objective)
            state.add_step(Step(kind=StepKind.DECISION, tool_call=tool_call, decision=decision))

            if decision.effect is not DecisionEffect.ALLOW:
                state.policy_denied = True
                if decision.effect is DecisionEffect.REQUIRES_APPROVAL:
                    state.needs_human = True
                observation = f"DENIED ({decision.matched_rule}): {decision.reason}"
                state.add_step(
                    Step(kind=StepKind.OBSERVATION, tool_call=tool_call, observation=observation)
                )
                state.messages.append(
                    ModelMessage(
                        role="user",
                        content=f"Tool '{call.name}' was denied by policy: {decision.reason}",
                    )
                )
                continue

            result = self._tools.dispatch(call.name, dict(call.arguments))
            state.add_step(
                Step(
                    kind=StepKind.TOOL_CALL,
                    tool_call=tool_call,
                    output=result.output if result.ok else None,
                )
            )
            observation = result.output if result.ok else (result.error or "tool error")
            state.add_step(
                Step(kind=StepKind.OBSERVATION, tool_call=tool_call, observation=observation)
            )
            state.messages.append(
                ModelMessage(role="user", content=f"Result of {call.name}: {observation}")
            )
            if not result.ok:
                state.tool_failures[call.name] = state.tool_failures.get(call.name, 0) + 1

        state.pending = []
        state.iteration += 1
        return state

    def _finalize_node(self, state: AgentState) -> AgentState:
        if state.output is None:
            state.output = state.last_content or None

        hr = state.objective.handoff_rules
        if hr is not None:
            if state.confidence is not None and state.confidence < hr.confidence_below:
                state.needs_human = True
                state.risks.append(
                    f"confidence {state.confidence} below threshold {hr.confidence_below}"
                )
            escalate_policy = hr.on_policy_conflict == "escalate" and self._escalate_on_denied
            if state.policy_denied and escalate_policy:
                state.needs_human = True
                state.risks.append("policy conflict: a tool call was denied")
            if state.tool_failures:
                worst = max(state.tool_failures.values())
                if worst > hr.on_test_failure_after_retries:
                    state.needs_human = True
                    state.risks.append(
                        f"tool failed {worst} times (>" f"{hr.on_test_failure_after_retries})"
                    )

        state.add_step(
            Step(kind=StepKind.OUTPUT, output=state.output, confidence=state.confidence)
        )
        if state.needs_human:
            state.add_step(
                Step(
                    kind=StepKind.HANDOFF,
                    observation="escalated to human",
                    metadata={"risks": list(state.risks)},
                )
            )
        return state

    # ------------------------------------------------------------------ #
    # Routing + finish handling                                          #
    # ------------------------------------------------------------------ #
    def _route(self, state: AgentState) -> str:
        if state.finished:
            return "finalize"
        if not state.pending:
            return "finalize"
        if state.iteration >= state.max_iterations:
            state.error = "max_iterations_exceeded"
            state.needs_human = True
            return "finalize"
        return "act"

    def _apply_refusal(self, state: AgentState, stop_reason: str | None) -> None:
        """Handle a provider ``refusal`` stop: escalate, no blind retry."""
        category = None
        if stop_reason and ":" in stop_reason:
            category = stop_reason.split(":", 1)[1].strip() or None
        risk = f"model refused: {category}" if category else "model refused"
        state.finished = True
        state.needs_human = True
        state.pending = []
        state.risks.append(risk)
        state.add_step(Step(kind=StepKind.OBSERVATION, observation=risk))

    def _apply_finish(self, state: AgentState, arguments: dict[str, Any]) -> None:
        output = arguments.get("output") or arguments.get("summary")
        if output is not None:
            state.output = str(output)
        summary = arguments.get("summary")
        state.summary = str(summary) if summary is not None else state.output
        confidence = arguments.get("confidence")
        if confidence is not None:
            state.confidence = float(confidence)
        accepted = arguments.get("acceptance_criteria_satisfied") or arguments.get("acceptance")
        if isinstance(accepted, list):
            state.acceptance_satisfied = [str(item) for item in accepted]
        if arguments.get("needs_human"):
            state.needs_human = True
        risks = arguments.get("risks")
        if isinstance(risks, list):
            state.risks.extend(str(item) for item in risks)

    # ------------------------------------------------------------------ #
    # Result assembly                                                    #
    # ------------------------------------------------------------------ #
    def _to_result(self, state: AgentState) -> AgentRunResult:
        if state.needs_human:
            status = RunStatus.ESCALATED
        elif state.error:
            status = RunStatus.FAILED
        else:
            status = RunStatus.SUCCEEDED
        artifacts = dict(state.artifacts)
        artifacts["iterations"] = state.iteration
        if state.risks:
            artifacts["risks"] = list(state.risks)
        # HARD-02: per-run token + derived cost, priced by the real model id the
        # provider reported (falls back to the objective's model, else "unknown").
        model = state.model_name or state.objective.model or "unknown"
        artifacts["model_usage"] = state.usage.to_artifact(model)
        return AgentRunResult(
            run_id=uuid.uuid4(),
            task_id=state.objective.task_id,
            status=status,
            steps=state.steps,
            output=state.output,
            summary=state.summary,
            confidence=state.confidence,
            needs_human=state.needs_human,
            acceptance_criteria_satisfied=state.acceptance_satisfied,
            artifacts=artifacts,
            error=state.error,
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _is_refusal(stop_reason: str | None) -> bool:
    """True for a provider safety stop (``"refusal"`` or ``"refusal:<category>"``)."""
    return bool(stop_reason) and str(stop_reason).split(":", 1)[0] == "refusal"


def _wants_worktree(objective: AgentObjective) -> bool:
    return any(target.worktree for target in objective.repo_targets)


def _base_branch(objective: AgentObjective) -> str:
    for target in objective.repo_targets:
        if target.worktree:
            return target.base_branch
    return "main"


def _branch_name(objective: AgentObjective) -> str:
    for target in objective.repo_targets:
        if target.worktree and target.branch_prefix:
            return f"{target.branch_prefix}-{uuid.uuid4().hex[:6]}"
    key = objective.key or "task"
    return f"forge/{key}-{uuid.uuid4().hex[:6]}"
