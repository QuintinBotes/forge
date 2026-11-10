"""One isolated ``claude --print`` run and the judge, against a fake executable.

``fake_claude.py`` beside this file stands in for Claude Code: no model, no
network. It echoes what it was given (argv, environment keys, HOME, the
working directory's origin, the MCP config's mode) so isolation is asserted
on what the child process actually saw.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from forge_eval.ctxbench.bank import SUITE_DIR, load_bank
from forge_eval.ctxbench.claude import (
    AGENT_EFFORT,
    AGENT_MODEL,
    JUDGE_MODEL,
    ClaudeCall,
    ClaudeResult,
    ask_problems,
    claude_version,
    mcp_config,
    parse_stream,
    run_claude,
)
from forge_eval.ctxbench.judge import JudgeError, judge, judge_prompt, parse_verdict
from forge_eval.ctxbench.prompts import load_prompts

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")
SUBJECT = "acme-example/lore"
ENV = {"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "sk-test", "LEAK": "x"}
BANK = load_bank(subject=SUBJECT)


def _run(tmp_path: Path, call: ClaudeCall) -> ClaudeResult:
    return run_claude(
        call,
        subject=SUBJECT,
        scratch=tmp_path / "scratch",
        env=ENV,
        transcript=tmp_path / "t.jsonl",
    )


def _init(result: ClaudeResult) -> dict:
    lines = result.transcript.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["fake"]


def _forge(prompt: str) -> ClaudeCall:
    servers = mcp_config("http://forge.invalid/mcp", "t")
    return ClaudeCall(model=AGENT_MODEL, prompt=prompt, mcp_servers=servers, claude_bin=FAKE)


def _none(prompt: str) -> ClaudeCall:
    return ClaudeCall(model=AGENT_MODEL, prompt=prompt, claude_bin=FAKE)


def test_a_forge_ask_is_isolated_and_parsed(tmp_path: Path) -> None:
    call = ClaudeCall(
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        prompt="Question: where?",
        mcp_servers=mcp_config("http://forge.invalid/api/v1/mcp", "tok-123"),
        claude_bin=FAKE,
    )
    result = _run(tmp_path, call)
    seen = _init(result)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    for flag in ("--print", "--verbose", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert "Question: where?" not in argv  # the prompt goes on stdin
    assert {"PATH", "HOME", "ANTHROPIC_API_KEY"} <= set(seen["env_keys"])
    assert "LEAK" not in seen["env_keys"]
    assert seen["origin"] == f"https://github.com/{SUBJECT}"
    assert seen["home_settings"] == "{}"
    assert seen["mcp_config_mode"] == "0o600"
    assert seen["authorization_sha256"] == [hashlib.sha256(b"Bearer tok-123").hexdigest()]
    assert list((tmp_path / "scratch").iterdir()) == []  # HOME, workdir and key file are gone
    assert result.mcp_servers == {"forge": "connected"}
    assert result.tool_calls == ("mcp__forge__assemble_context",)
    assert result.envelopes == (
        {"legs": ["bm25", "vector"], "vector_leg": "configured", "rerank": "off"},
    )
    assert result.usage.total == 100 + 20 + 3000 + 500
    assert (result.cost_usd, result.duration_s, result.turns) == (0.02, 2.5, 2)
    assert result.error is None and not result.guard_tripped
    assert ask_problems(result, expect_forge=True, expected_legs=["bm25", "vector"]) == []
    assert ask_problems(result, expect_forge=True, expected_legs=["bm25"]) == [
        "retrieval legs ['bm25', 'vector'] != ['bm25']"
    ]


def test_mcp_failure_or_missing_server_makes_an_ask_invalid(tmp_path: Path) -> None:
    down = ClaudeCall(
        model=AGENT_MODEL,
        prompt="Question: q",
        mcp_servers=mcp_config("http://unreachable.invalid/mcp", "t"),
        claude_bin=FAKE,
    )
    result = _run(tmp_path, down)
    assert ask_problems(result, expect_forge=True) == ["mcp server forge failed"]
    none = _run(tmp_path, ClaudeCall(model=AGENT_MODEL, prompt="Question: q", claude_bin=FAKE))
    assert ask_problems(none, expect_forge=True) == ["mcp server forge missing"]
    assert ask_problems(none, expect_forge=False) == []
    fallback = ClaudeCall(
        model=AGENT_MODEL,
        prompt="Question: FALLBACK",
        mcp_servers=mcp_config("http://forge.invalid/mcp", "t"),
        claude_bin=FAKE,
    )
    assert ask_problems(_run(tmp_path, fallback), expect_forge=True) == ["reranker fell back"]


def test_a_forge_tool_error_makes_an_ask_invalid(tmp_path: Path) -> None:
    result = _run(tmp_path, _forge("Question: TOOLERROR"))
    assert result.error is None  # the run itself ended well: only the tool failed
    assert ask_problems(result, expect_forge=True) == [
        "forge tool error: mcp__forge__assemble_context: Error: HTTP 502 Bad Gateway"
    ]
    # forge's own isError payload, on a block that carries no is_error flag
    uses = [
        {"type": "tool_use", "id": "a", "name": "mcp__forge__search_knowledge"},
        {"type": "tool_use", "id": "b", "name": "mcp__forge__search_knowledge"},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "a", "content": '{"error": "no such workspace"}'},
        {"type": "tool_result", "tool_use_id": "b", "content": '{"error": "", "legs": []}'},
    ]
    lines = [
        json.dumps({"type": "assistant", "message": {"content": uses}}),
        json.dumps({"type": "user", "message": {"content": results}}),
    ]
    assert parse_stream(lines)["tool_errors"] == [
        'mcp__forge__search_knowledge: {"error": "no such workspace"}'
    ]


def test_the_none_arm_with_a_server_and_a_rerank_mismatch_are_invalid(tmp_path: Path) -> None:
    result = _run(tmp_path, _forge("Question: q"))
    assert ask_problems(result, expect_forge=False) == [
        "unexpected tools ['mcp__forge__assemble_context']",
        "unexpected mcp servers ['forge']",
    ]
    assert ask_problems(result, expect_forge=True, expected_rerank="off") == []
    assert ask_problems(result, expect_forge=True, expected_rerank="on") == ["rerank off != on"]


def test_only_forge_tools_may_be_listed(tmp_path: Path) -> None:
    stray = _run(tmp_path, _forge("Question: STRAYTOOL"))
    assert ask_problems(stray, expect_forge=True) == ["unexpected tools ['Bash']"]
    assert ask_problems(_run(tmp_path, _none("Question: STRAYTOOL")), expect_forge=False) == [
        "unexpected tools ['Bash']"
    ]


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        '"text"',
        '{"type": "assistant", "message": "oops"}',
        '{"type": "user", "message": {"content": [1]}}',
        '{"type": "system", "subtype": "init", "mcp_servers": {"forge": "connected"}}',
        '{"type": "system", "subtype": "init", "tools": "Bash"}',
        '{"type": "result", "subtype": "success", "usage": [1, 2]}',
        '{"type": "result", "subtype": "success", "usage": {"input_tokens": "n/a"}}',
    ],
)
def test_a_stream_event_of_the_wrong_shape_is_a_warning_never_a_crash(line: str) -> None:
    good = '{"type": "result", "subtype": "success", "result": "ok"}'
    parsed = parse_stream([line, good])
    assert (parsed["text"], parsed["error"]) == ("ok", None)  # the other events still count
    assert len(parsed["warnings"]) == 1
    assert parsed["warnings"][0].startswith("stream line 1: ")


def test_a_malformed_stream_event_makes_an_ask_invalid(tmp_path: Path) -> None:
    result = _run(tmp_path, _none("Question: MALFORMED"))
    assert result.error is None
    assert ask_problems(result, expect_forge=False) == [
        "parse warning: stream line 2: TypeError: assistant message is a str, not a dict"
    ]


@pytest.mark.parametrize("subtype", ["error_max_budget_usd", "error_max_turns"])
def test_a_budget_or_turn_limit_stop_is_a_guard_trip(tmp_path: Path, subtype: str) -> None:
    result = _run(tmp_path, _none(f"Question: STOP:{subtype}"))
    assert result.guard_tripped
    assert result.error is None  # scored on what it returned, never retried
    assert "I don't know" in result.text
    assert ask_problems(result, expect_forge=False) == []


def test_any_other_failed_result_stays_an_error(tmp_path: Path) -> None:
    result = _run(tmp_path, _none("Question: STOP:error_during_execution"))
    assert not result.guard_tripped
    assert ask_problems(result, expect_forge=False) == ["result error_during_execution"]


def test_a_missing_binary_or_result_is_an_error(tmp_path: Path) -> None:
    gone = _run(tmp_path, ClaudeCall(model=AGENT_MODEL, prompt="q", claude_bin=FAKE + ".nope"))
    assert gone.error is not None
    assert ask_problems(gone, expect_forge=False) == [gone.error]
    assert parse_stream(["not json", "[]", '{"type": "system"}'])["error"] == "no result event"


def test_the_wall_clock_guard_kills_and_flags(tmp_path: Path) -> None:
    call = ClaudeCall(
        model=AGENT_MODEL,
        prompt="Question: SLOW",
        mcp_servers=mcp_config("http://forge.invalid/mcp", "t"),
        timeout_s=2,
        claude_bin=FAKE,
    )
    result = _run(tmp_path, call)
    assert result.guard_tripped
    assert result.error is None  # scored on what it returned, never retried
    assert result.mcp_servers == {"forge": "connected"}  # the init event before the kill
    assert result.text == ""


def test_claude_version() -> None:
    assert claude_version(FAKE) == "9.9.9 (Claude Code, fake)"
    assert claude_version("/nonexistent/claude") == ""


def test_parse_verdict_is_strict() -> None:
    assert parse_verdict('{"verdict": "partial", "reason": "r"}') == ("partial", "r")
    assert parse_verdict('```json\n{"verdict": "dont_know", "reason": "r"}\n```')[0] == "dont_know"
    for bad in (
        "correct",
        '{"verdict": "abstained", "reason": "r"}',
        '{"verdict": "correct"}',
        '{"verdict": "correct", "reason": "r", "score": 1}',
        'Sure: {"verdict": "correct", "reason": "r"}',
        '{"verdict": "correct", "reason": 1}',
    ):
        with pytest.raises(JudgeError):
            parse_verdict(bad)


def test_judge_through_the_fake(tmp_path: Path) -> None:
    rubric = load_prompts(SUITE_DIR).judge
    question = BANK.by_id()["lore-dec-02"]
    prompt = judge_prompt(rubric, question, "It is {gold}.")
    assert "It is {gold}." in prompt  # the answer is never expanded
    assert "Answerable: yes" in prompt and question.gold in prompt

    def runner(call: ClaudeCall) -> ClaudeResult:
        assert call.model == JUDGE_MODEL and dict(call.mcp_servers) == {}
        return _run(tmp_path, call)

    graded = judge(question, "Over one WebSocket.", rubric=rubric, runner=runner, claude_bin=FAKE)
    assert (graded.verdict, graded.reason, graded.model) == ("correct", "fake judge", JUDGE_MODEL)
    assert judge(
        question, "I don't know", rubric=rubric, runner=runner, claude_bin=FAKE
    ).verdict == ("dont_know")
    with pytest.raises(JudgeError):
        judge(question, "BADJUDGE", rubric=rubric, runner=runner, claude_bin=FAKE)
    # the fake judge refuses an <answer> block that holds more than the answer
    row = json.dumps({"answer": "Over one WebSocket.", "sources": ["adrs/ADR-001.md"]})
    with pytest.raises(JudgeError, match="no result event"):
        judge(question, row, rubric=rubric, runner=runner, claude_bin=FAKE)
