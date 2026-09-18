"""The typed Key-Metric facade + frozen instrument catalog (F38 §4).

``ForgeMetrics`` is the ONLY way feature slices touch metrics: one typed method
per spec Key Metric, mapped onto the frozen Prometheus instrument catalog below
with **bounded label cardinality**. High-cardinality dimensions (task ids, run
ids, user ids, raw workspace ids) are rejected by construction — they are not
parameters of any facade method and the guard strips them if smuggled in.

Implementations:

- :class:`RecordingMetrics` — the in-process implementation. It keeps real
  counter/histogram/gauge values keyed by (bounded) label sets, is directly
  assertable in tests, and renders the Prometheus text exposition for the
  internal scrape endpoint. When the OpenTelemetry SDK dependency lands, this
  registry becomes the bridge's local mirror; the facade contract is unchanged.
- :class:`NoopMetrics` — the degraded mode (``OBS_ENABLED=false``): every method
  is a cheap no-op so producers call the facade unconditionally (spec AC18).

The process singleton is owned by :func:`get_metrics` / :func:`set_metrics`;
:func:`~forge_obs.telemetry.setup_telemetry` installs the right implementation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

__all__ = [
    "CARDINALITY_CAP",
    "FORBIDDEN_LABELS",
    "INSTRUMENT_CATALOG",
    "ForgeMetrics",
    "Instrument",
    "NoopMetrics",
    "RecordingMetrics",
    "get_metrics",
    "render_prometheus",
    "reset_metrics",
    "set_metrics",
]

# --------------------------------------------------------------------------- #
# Frozen instrument catalog (spec §4) — names, kinds, and allowed label keys.  #
# --------------------------------------------------------------------------- #


class Instrument:
    """One catalog entry: Prometheus name, kind, and its bounded label keys."""

    __slots__ = ("kind", "labels", "name")

    def __init__(self, name: str, kind: str, labels: tuple[str, ...]) -> None:
        self.name = name
        self.kind = kind  # counter | histogram | gauge
        self.labels = labels


INSTRUMENT_CATALOG: dict[str, Instrument] = {
    i.name: i
    for i in (
        # --- Cost ---
        Instrument(
            "forge_model_tokens_total",
            "counter",
            ("service", "provider", "model", "phase", "token_kind"),
        ),
        Instrument(
            "forge_model_cost_usd_total", "counter", ("service", "provider", "model", "phase")
        ),
        Instrument("forge_cost_emit_failures_total", "counter", ("service", "reason")),
        Instrument("forge_unpriced_model_total", "counter", ("provider", "model")),
        # --- Workflow quality ---
        Instrument("forge_workflow_runs_total", "counter", ("workflow", "terminal_state")),
        Instrument("forge_task_completions_total", "counter", ("status",)),
        Instrument("forge_approval_decisions_total", "counter", ("gate", "decision")),
        Instrument("forge_pr_outcomes_total", "counter", ("outcome",)),
        Instrument("forge_time_to_merge_seconds", "histogram", ("workflow",)),
        Instrument("forge_task_duration_seconds", "histogram", ("workflow",)),
        Instrument("forge_spec_completeness", "histogram", ()),
        # --- Agent quality ---
        Instrument("forge_agent_runs_total", "counter", ("status", "skill_profile")),
        Instrument("forge_agent_retries_total", "counter", ("skill_profile",)),
        Instrument("forge_agent_confidence", "histogram", ("skill_profile",)),
        Instrument("forge_requirement_satisfaction_ratio", "histogram", ()),
        # --- Retrieval quality ---
        Instrument("forge_retrieval_requests_total", "counter", ("hit",)),
        Instrument("forge_retrieval_latency_seconds", "histogram", ("stage",)),
        Instrument("forge_reranker_delta", "histogram", ()),
        Instrument("forge_mcp_calls_total", "counter", ("connection", "status")),
        Instrument("forge_mcp_call_latency_seconds", "histogram", ("connection",)),
        Instrument("forge_mcp_freshness_lag_seconds", "gauge", ("connection",)),
    )
}

#: Label keys that must NEVER appear on a Prometheus series (spec AC11).
#: ``workspace_id`` is additionally allowed only behind OBS_METRIC_WORKSPACE_LABEL,
#: and even then it is not part of the frozen catalog above.
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {"task_id", "workflow_run_id", "user_id", "workspace_id", "agent_run_id", "step_id"}
)

#: Retrieval stages the catalog bounds ``stage`` to.
RETRIEVAL_STAGES: frozenset[str] = frozenset({"semantic", "keyword", "fusion", "rerank", "total"})

#: Hard per-label distinct-value cap: past this, new values bucket to "other".
CARDINALITY_CAP = 100

_OTHER = "other"


# --------------------------------------------------------------------------- #
# Facade protocol                                                              #
# --------------------------------------------------------------------------- #


@runtime_checkable
class ForgeMetrics(Protocol):
    """One typed method per spec Key Metric family (frozen contract, F38 §4)."""

    # --- Cost (called only via the UsageMeter) ---
    def record_model_cost(
        self,
        *,
        provider: str,
        model: str,
        kind: str,
        phase: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None: ...

    def record_unpriced_model(self, *, provider: str, model: str) -> None: ...

    def record_cost_emit_failure(self, *, reason: str) -> None: ...

    # --- Workflow quality ---
    def record_workflow_terminal(
        self, *, workflow: str, terminal_state: str, duration_seconds: float
    ) -> None: ...

    def record_task_completion(self, *, status: str, duration_seconds: float | None) -> None: ...

    def record_approval_decision(self, *, gate: str, decision: str) -> None: ...

    def record_pr_outcome(self, *, outcome: str, time_to_merge_seconds: float | None) -> None: ...

    def observe_spec_completeness(self, *, score: float) -> None: ...

    # --- Agent quality ---
    def record_agent_run(
        self, *, status: str, skill_profile: str, retries: int, confidence: float | None
    ) -> None: ...

    def observe_requirement_satisfaction(self, *, ratio: float) -> None: ...

    # --- Retrieval quality ---
    def record_retrieval(
        self,
        *,
        hit: bool,
        total_latency_seconds: float,
        stage_latencies: Mapping[str, float],
        reranker_delta: float | None,
    ) -> None: ...

    def record_mcp_call(self, *, connection: str, status: str, latency_seconds: float) -> None: ...

    def set_mcp_freshness_lag(self, *, connection: str, lag_seconds: float) -> None: ...


class NoopMetrics:
    """Degraded-mode facade: every recorder is a no-op (spec AC1/AC18).

    The protocol members are declared explicitly (``isinstance`` protocol checks
    use ``getattr_static`` and never see ``__getattr__``); the catch-all keeps
    forward-compat with facade methods added later.
    """

    def _noop(self, **_kwargs: object) -> None:
        return None

    record_model_cost = _noop
    record_unpriced_model = _noop
    record_cost_emit_failure = _noop
    record_workflow_terminal = _noop
    record_task_completion = _noop
    record_approval_decision = _noop
    record_pr_outcome = _noop
    observe_spec_completeness = _noop
    record_agent_run = _noop
    observe_requirement_satisfaction = _noop
    record_retrieval = _noop
    record_mcp_call = _noop
    set_mcp_freshness_lag = _noop

    def __getattr__(self, name: str) -> Callable[..., None]:
        # Uniform no-op surface for facade methods added later.
        if name.startswith(("record_", "observe_", "set_")):
            return self._noop
        raise AttributeError(name)


# --------------------------------------------------------------------------- #
# Recording implementation                                                     #
# --------------------------------------------------------------------------- #

LabelSet = tuple[tuple[str, str], ...]


class RecordingMetrics:
    """In-process ``ForgeMetrics`` with real values + the cardinality guard.

    ``allowed_values`` optionally pins a label key to an allow-list; a value
    outside it is bucketed to ``"other"`` (spec AC11). Independently, every
    label key is capped at :data:`CARDINALITY_CAP` distinct values.
    """

    def __init__(
        self,
        *,
        service: str = "forge",
        allowed_values: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self._service = service
        self._allowed = {k: frozenset(v) for k, v in (allowed_values or {}).items()}
        self._seen: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self.counters: dict[str, dict[LabelSet, float]] = {}
        self.histograms: dict[str, dict[LabelSet, list[float]]] = {}
        self.gauges: dict[str, dict[LabelSet, float]] = {}

    # ---- guard ------------------------------------------------------------ #

    def _bound(self, key: str, value: str) -> str:
        allowed = self._allowed.get(key)
        if allowed is not None and value not in allowed:
            return _OTHER
        seen = self._seen.setdefault(key, set())
        if value not in seen:
            if len(seen) >= CARDINALITY_CAP:
                return _OTHER
            seen.add(value)
        return value

    def _labels(self, instrument: Instrument, **labels: str | None) -> LabelSet:
        out: list[tuple[str, str]] = []
        for key in instrument.labels:
            if key == "service":
                out.append(("service", self._service))
                continue
            raw = labels.get(key)
            if key in FORBIDDEN_LABELS:  # defense in depth; catalog never lists these
                continue
            value = "" if raw is None else str(raw)
            out.append((key, self._bound(key, value)))
        return tuple(out)

    # ---- primitive sinks --------------------------------------------------- #

    def _inc(self, name: str, amount: float, **labels: str | None) -> None:
        instrument = INSTRUMENT_CATALOG[name]
        label_set = self._labels(instrument, **labels)
        with self._lock:
            series = self.counters.setdefault(name, {})
            series[label_set] = series.get(label_set, 0.0) + amount

    def _observe(self, name: str, value: float, **labels: str | None) -> None:
        instrument = INSTRUMENT_CATALOG[name]
        label_set = self._labels(instrument, **labels)
        with self._lock:
            self.histograms.setdefault(name, {}).setdefault(label_set, []).append(value)

    def _set(self, name: str, value: float, **labels: str | None) -> None:
        instrument = INSTRUMENT_CATALOG[name]
        label_set = self._labels(instrument, **labels)
        with self._lock:
            self.gauges.setdefault(name, {})[label_set] = value

    # ---- test/scrape helpers ------------------------------------------------ #

    @staticmethod
    def _match(label_set: LabelSet, subset: Mapping[str, str]) -> bool:
        as_dict = dict(label_set)
        return all(as_dict.get(k) == v for k, v in subset.items())

    def counter_value(self, name: str, **labels: str) -> float:
        return sum(v for ls, v in self.counters.get(name, {}).items() if self._match(ls, labels))

    def histogram_values(self, name: str, **labels: str) -> list[float]:
        out: list[float] = []
        for ls, values in self.histograms.get(name, {}).items():
            if self._match(ls, labels):
                out.extend(values)
        return out

    def gauge_value(self, name: str, **labels: str) -> float | None:
        for ls, value in self.gauges.get(name, {}).items():
            if self._match(ls, labels):
                return value
        return None

    def label_values(self, name: str, key: str) -> set[str]:
        """Distinct recorded values of ``key`` on instrument ``name``."""
        series: Mapping[LabelSet, object] = (
            self.counters.get(name) or self.histograms.get(name) or self.gauges.get(name) or {}
        )
        return {dict(ls)[key] for ls in series if key in dict(ls)}

    # ---- ForgeMetrics facade ------------------------------------------------ #

    def record_model_cost(
        self,
        *,
        provider: str,
        model: str,
        kind: str,  # reserved; deliberately kept off labels to bound cardinality
        phase: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        common = {"provider": provider, "model": model, "phase": phase or "unknown"}
        self._inc("forge_model_tokens_total", prompt_tokens, token_kind="prompt", **common)
        self._inc("forge_model_tokens_total", completion_tokens, token_kind="completion", **common)
        self._inc("forge_model_cost_usd_total", cost_usd, **common)

    def record_unpriced_model(self, *, provider: str, model: str) -> None:
        self._inc("forge_unpriced_model_total", 1, provider=provider, model=model)

    def record_cost_emit_failure(self, *, reason: str) -> None:
        self._inc("forge_cost_emit_failures_total", 1, reason=reason)

    def record_workflow_terminal(
        self, *, workflow: str, terminal_state: str, duration_seconds: float
    ) -> None:
        self._inc("forge_workflow_runs_total", 1, workflow=workflow, terminal_state=terminal_state)
        self._observe("forge_task_duration_seconds", duration_seconds, workflow=workflow)

    def record_task_completion(self, *, status: str, duration_seconds: float | None) -> None:
        self._inc("forge_task_completions_total", 1, status=status)
        if duration_seconds is not None:
            self._observe("forge_task_duration_seconds", duration_seconds, workflow="task")

    def record_approval_decision(self, *, gate: str, decision: str) -> None:
        self._inc("forge_approval_decisions_total", 1, gate=gate, decision=decision)

    def record_pr_outcome(self, *, outcome: str, time_to_merge_seconds: float | None) -> None:
        self._inc("forge_pr_outcomes_total", 1, outcome=outcome)
        if time_to_merge_seconds is not None:
            self._observe(
                "forge_time_to_merge_seconds", time_to_merge_seconds, workflow="default_feature"
            )

    def observe_spec_completeness(self, *, score: float) -> None:
        self._observe("forge_spec_completeness", score)

    def record_agent_run(
        self, *, status: str, skill_profile: str, retries: int, confidence: float | None
    ) -> None:
        self._inc("forge_agent_runs_total", 1, status=status, skill_profile=skill_profile)
        if retries:
            self._inc("forge_agent_retries_total", retries, skill_profile=skill_profile)
        if confidence is not None:
            self._observe("forge_agent_confidence", confidence, skill_profile=skill_profile)

    def observe_requirement_satisfaction(self, *, ratio: float) -> None:
        self._observe("forge_requirement_satisfaction_ratio", ratio)

    def record_retrieval(
        self,
        *,
        hit: bool,
        total_latency_seconds: float,
        stage_latencies: Mapping[str, float],
        reranker_delta: float | None,
    ) -> None:
        self._inc("forge_retrieval_requests_total", 1, hit="true" if hit else "false")
        self._observe("forge_retrieval_latency_seconds", total_latency_seconds, stage="total")
        for stage, latency in stage_latencies.items():
            bounded = stage if stage in RETRIEVAL_STAGES else _OTHER
            self._observe("forge_retrieval_latency_seconds", latency, stage=bounded)
        if reranker_delta is not None:
            self._observe("forge_reranker_delta", reranker_delta)

    def record_mcp_call(self, *, connection: str, status: str, latency_seconds: float) -> None:
        self._inc("forge_mcp_calls_total", 1, connection=connection, status=status)
        self._observe("forge_mcp_call_latency_seconds", latency_seconds, connection=connection)

    def set_mcp_freshness_lag(self, *, connection: str, lag_seconds: float) -> None:
        self._set("forge_mcp_freshness_lag_seconds", lag_seconds, connection=connection)


# --------------------------------------------------------------------------- #
# Prometheus text exposition (internal scrape endpoint)                        #
# --------------------------------------------------------------------------- #


def _fmt_labels(label_set: LabelSet) -> str:
    if not label_set:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in label_set)
    return "{" + inner + "}"


def render_prometheus(metrics: RecordingMetrics) -> str:
    """Render the recorded registry in the Prometheus text exposition format."""
    lines: list[str] = []
    for name, series in sorted(metrics.counters.items()):
        lines.append(f"# TYPE {name} counter")
        for label_set, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(label_set)} {value}")
    for name, series in sorted(metrics.gauges.items()):
        lines.append(f"# TYPE {name} gauge")
        for label_set, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(label_set)} {value}")
    for name, hseries in sorted(metrics.histograms.items()):
        lines.append(f"# TYPE {name} histogram")
        for label_set, values in sorted(hseries.items()):
            base = dict(label_set)
            count_labels = tuple(sorted(base.items()))
            lines.append(f"{name}_count{_fmt_labels(count_labels)} {len(values)}")
            lines.append(f"{name}_sum{_fmt_labels(count_labels)} {sum(values)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Process singleton                                                            #
# --------------------------------------------------------------------------- #

_metrics_lock = threading.Lock()
_metrics: ForgeMetrics = NoopMetrics()


def get_metrics() -> ForgeMetrics:
    """Return the process metrics facade (real or no-op; never raises)."""
    return _metrics


def set_metrics(metrics: ForgeMetrics) -> ForgeMetrics:
    """Install ``metrics`` as the process singleton and return it."""
    global _metrics
    with _metrics_lock:
        _metrics = metrics
    return metrics


def reset_metrics() -> None:
    """Reset the singleton to the no-op facade (test isolation)."""
    set_metrics(NoopMetrics())
