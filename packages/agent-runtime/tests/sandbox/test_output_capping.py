"""AC10 — output capping + artifact offload (pure helper)."""

from __future__ import annotations

from _sandbox_fakes import FakeObjectStore

from forge_agent.sandbox.output import cap_output


def test_under_cap_unchanged() -> None:
    text = "short output"
    inline, ref = cap_output(text, cap_bytes=1024, store=None, key="k")
    assert inline == text
    assert ref is None


def test_over_cap_truncates_and_offloads() -> None:
    store = FakeObjectStore()
    text = "x" * 5000
    inline, ref = cap_output(text, cap_bytes=256, store=store, key="sandbox/run/stdout.log")
    assert len(inline.encode("utf-8")) <= 256
    assert ref == "minio://artifacts/sandbox/run/stdout.log"
    assert store.objects["sandbox/run/stdout.log"] == text.encode("utf-8")


def test_over_cap_without_store_truncates_no_ref() -> None:
    text = "y" * 5000
    inline, ref = cap_output(text, cap_bytes=100, store=None, key="k")
    assert len(inline.encode("utf-8")) <= 100
    assert ref is None
