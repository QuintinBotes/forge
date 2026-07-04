"""ForgeMetrics facade + cardinality contract tests (F38 AC11)."""

from __future__ import annotations

import pytest

from forge_obs.metrics import (
    CARDINALITY_CAP,
    FORBIDDEN_LABELS,
    INSTRUMENT_CATALOG,
    ForgeMetrics,
    NoopMetrics,
    RecordingMetrics,
    get_metrics,
    render_prometheus,
    reset_metrics,
    set_metrics,
)


@pytest.fixture(autouse=True)
def _isolate_singleton():
    yield
    reset_metrics()


def test_catalog_has_no_high_cardinality_labels() -> None:
    """AC11: no task_id/workflow_run_id/user_id/workspace_id label anywhere."""
    for instrument in INSTRUMENT_CATALOG.values():
        assert not (set(instrument.labels) & FORBIDDEN_LABELS), instrument.name


def test_facade_methods_emit_expected_instruments() -> None:
    m = RecordingMetrics(service="forge-api")

    m.record_model_cost(
        provider="anthropic",
        model="claude-sonnet-4-5",
        kind="completion",
        phase="executing",
        prompt_tokens=2000,
        completion_tokens=500,
        cost_usd=0.0135,
    )
    assert m.counter_value("forge_model_tokens_total", token_kind="prompt") == 2000
    assert m.counter_value("forge_model_tokens_total", token_kind="completion") == 500
    assert m.counter_value(
        "forge_model_cost_usd_total", provider="anthropic", phase="executing"
    ) == pytest.approx(0.0135)
    # The service label is stamped from the telemetry init, not the caller.
    assert m.label_values("forge_model_cost_usd_total", "service") == {"forge-api"}

    m.record_workflow_terminal(
        workflow="default_feature", terminal_state="done", duration_seconds=42.0
    )
    assert m.counter_value("forge_workflow_runs_total", terminal_state="done") == 1
    assert m.histogram_values("forge_task_duration_seconds", workflow="default_feature") == [42.0]

    m.record_task_completion(status="completed", duration_seconds=None)
    assert m.counter_value("forge_task_completions_total", status="completed") == 1

    m.record_approval_decision(gate="spec_approval", decision="approved")
    assert m.counter_value("forge_approval_decisions_total", gate="spec_approval") == 1

    m.record_pr_outcome(outcome="merged", time_to_merge_seconds=3600.0)
    assert m.counter_value("forge_pr_outcomes_total", outcome="merged") == 1
    assert m.histogram_values("forge_time_to_merge_seconds") == [3600.0]

    m.observe_spec_completeness(score=0.9)
    assert m.histogram_values("forge_spec_completeness") == [0.9]

    m.record_agent_run(status="succeeded", skill_profile="backend-tdd", retries=2, confidence=0.8)
    assert m.counter_value("forge_agent_runs_total", status="succeeded") == 1
    assert m.counter_value("forge_agent_retries_total", skill_profile="backend-tdd") == 2
    assert m.histogram_values("forge_agent_confidence") == [0.8]

    m.observe_requirement_satisfaction(ratio=0.75)
    assert m.histogram_values("forge_requirement_satisfaction_ratio") == [0.75]

    m.record_retrieval(
        hit=True,
        total_latency_seconds=0.5,
        stage_latencies={"semantic": 0.04, "rerank": 0.41, "bogus_stage": 0.01},
        reranker_delta=0.2,
    )
    assert m.counter_value("forge_retrieval_requests_total", hit="true") == 1
    assert m.histogram_values("forge_retrieval_latency_seconds", stage="total") == [0.5]
    # Unknown stage is bounded to "other", never a new label value.
    assert m.histogram_values("forge_retrieval_latency_seconds", stage="other") == [0.01]
    assert m.histogram_values("forge_reranker_delta") == [0.2]

    m.record_mcp_call(connection="github", status="ok", latency_seconds=0.2)
    assert m.counter_value("forge_mcp_calls_total", connection="github", status="ok") == 1

    m.set_mcp_freshness_lag(connection="github", lag_seconds=30.0)
    assert m.gauge_value("forge_mcp_freshness_lag_seconds", connection="github") == 30.0


def test_unknown_label_value_bucketed_to_other_with_allow_list() -> None:
    m = RecordingMetrics(
        service="t", allowed_values={"skill_profile": frozenset({"backend-tdd"})}
    )
    m.record_agent_run(status="failed", skill_profile="totally-new", retries=0, confidence=None)
    assert m.counter_value("forge_agent_runs_total", skill_profile="other") == 1
    assert m.label_values("forge_agent_runs_total", "skill_profile") == {"other"}


def test_cardinality_cap_buckets_overflow_to_other() -> None:
    m = RecordingMetrics(service="t")
    for i in range(CARDINALITY_CAP + 10):
        m.record_mcp_call(connection=f"conn-{i}", status="ok", latency_seconds=0.01)
    values = m.label_values("forge_mcp_calls_total", "connection")
    assert "other" in values
    assert len(values) <= CARDINALITY_CAP + 1  # cap distinct + the "other" bucket
    assert m.counter_value("forge_mcp_calls_total", connection="other") == 10


def test_noop_metrics_accepts_every_facade_call() -> None:
    noop = NoopMetrics()
    assert isinstance(noop, ForgeMetrics)
    noop.record_model_cost(
        provider="p", model="m", kind="completion", phase=None,
        prompt_tokens=1, completion_tokens=1, cost_usd=0.0,
    )
    noop.set_mcp_freshness_lag(connection="c", lag_seconds=1.0)
    with pytest.raises(AttributeError):
        _ = noop.not_a_metric_method


def test_singleton_default_is_noop_and_settable() -> None:
    reset_metrics()
    assert isinstance(get_metrics(), NoopMetrics)
    real = RecordingMetrics(service="t")
    set_metrics(real)
    assert get_metrics() is real


def test_render_prometheus_exposition() -> None:
    m = RecordingMetrics(service="forge-api")
    m.record_task_completion(status="completed", duration_seconds=1.5)
    text = render_prometheus(m)
    assert "# TYPE forge_task_completions_total counter" in text
    assert 'forge_task_completions_total{status="completed"} 1.0' in text
    assert "forge_task_duration_seconds_count" in text
