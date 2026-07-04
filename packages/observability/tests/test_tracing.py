"""traced() span correlation + attribute redaction (F38 AC12/AC13 substrate)."""

from __future__ import annotations

import pytest

from forge_obs.tracing import SpanStore, current_span_id, current_trace_id, traced


def test_nested_spans_share_trace_id_and_link_parent() -> None:
    store = SpanStore()
    with traced("api.request", store=store) as outer:
        assert current_trace_id() == outer.trace_id
        with traced("worker.task", store=store) as middle:
            assert middle.trace_id == outer.trace_id
            assert middle.parent_span_id == outer.span_id
            with traced("mcp.call", store=store) as inner:
                assert inner.trace_id == outer.trace_id
                assert inner.parent_span_id == middle.span_id
    # One trace across the three "services" (spec AC13, in-process substrate).
    assert {s.trace_id for s in store.spans()} == {outer.trace_id}
    assert current_trace_id() is None and current_span_id() is None


def test_span_attributes_are_redacted() -> None:
    store = SpanStore()
    with traced(
        "model.call",
        store=store,
        api_key="sk-anthropic123456789012345",
        task_id="t-1",
    ) as span:
        pass
    assert "sk-anthropic123456789012345" not in str(span.attributes)
    assert span.attributes["task_id"] == "t-1"  # high-cardinality ids belong on spans


def test_span_error_status_and_always_recorded() -> None:
    store = SpanStore()
    with pytest.raises(RuntimeError), traced("boom", store=store):
        raise RuntimeError("nope")
    (span,) = store.spans()
    assert span.status == "error"
    assert span.duration_s is not None
