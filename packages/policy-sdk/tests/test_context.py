"""F29 — :class:`PolicyContext` flattening + redaction (AC17 redaction)."""

from __future__ import annotations

from datetime import UTC, datetime

from forge_contracts import ToolCall
from forge_policy import PolicyContext, build_context_from_run


def test_to_fields_derives_action_path_ext_command() -> None:
    ctx = PolicyContext(branch="feature/x", environment=None)
    call = ToolCall(tool="write_file", path="infra/main.tf", arguments={"command": "ls"})
    fields = ctx.to_fields(call)
    assert fields["action"] == "write_file"
    assert fields["path"] == "infra/main.tf"
    assert fields["file_ext"] == "tf"
    assert fields["branch"] == "feature/x"
    # command falls back to the call arguments when not on the context.
    assert fields["command"] == "ls"


def test_to_fields_derives_weekday_hour_utc() -> None:
    ctx = PolicyContext(now=datetime(2026, 6, 23, 12, 30, tzinfo=UTC))  # Tuesday
    fields = ctx.to_fields(ToolCall(tool="deploy"))
    assert fields["weekday"] == 1  # Tue (0=Mon)
    assert fields["hour"] == 12
    assert fields["now"] == ctx.now


def test_to_fields_naive_now_assumed_utc() -> None:
    ctx = PolicyContext(now=datetime(2026, 6, 23, 9, 0))  # naive
    fields = ctx.to_fields(ToolCall(tool="deploy"))
    assert fields["hour"] == 9
    assert fields["weekday"] == 1


def test_to_fields_none_now_yields_none_derivations() -> None:
    fields = PolicyContext().to_fields(ToolCall(tool="deploy"))
    assert fields["now"] is None
    assert fields["weekday"] is None
    assert fields["hour"] is None


def test_to_redacted_fields_drops_command() -> None:
    ctx = PolicyContext(
        branch="main",
        environment="production",
        command="export TOKEN=secret && deploy",
        actor_role="admin",
        now=datetime(2026, 6, 23, 12, tzinfo=UTC),
    )
    redacted = ctx.to_redacted_fields()
    assert "command" not in redacted
    assert redacted["branch"] == "main"
    assert redacted["environment"] == "production"
    assert redacted["actor_role"] == "admin"
    assert redacted["now"] == "2026-06-23T12:00:00+00:00"
    # Audit projection must be JSON-serialisable.
    import json

    json.dumps(redacted)


def test_build_context_from_run() -> None:
    now = datetime(2026, 6, 23, 12, tzinfo=UTC)
    ctx = build_context_from_run(
        branch="feature/x",
        base_branch="main",
        environment="dev",
        task_kind="feature",
        actor_role="member",
        skill_profile="backend-tdd",
        execution_mode="single_agent",
        repo_id="github.com/org/api",
        now=now,
        labels=["p1"],
    )
    assert ctx.branch == "feature/x"
    assert ctx.base_branch == "main"
    assert ctx.task_kind == "feature"
    assert ctx.actor_role == "member"
    assert ctx.execution_mode == "single_agent"
    assert ctx.labels == ["p1"]
    assert ctx.now == now
