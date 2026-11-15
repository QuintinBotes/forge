"""One isolated ``claude --print`` run per ask or grade (spec J §2.4.1).

Each call is a fresh process with nothing of the operator's setup: an empty
temporary HOME holding only ``.claude/settings.json = {}``, an empty ``git
init`` working directory whose ``origin`` names the subject, no built-in
tools (``--tools ""``), only the MCP servers of the call's config
(``--strict-mcp-config``) and no session persistence. The environment holds
only ``PATH`` and ``ANTHROPIC_API_KEY``. A wall-clock timeout kills the
process; the call is then flagged ``guard_tripped`` and scored on whatever it
returned, as is a run the CLI stops at its budget or turn limit.

The MCP config (which holds the benchmark's read-only key) is written to a
0600 file inside the call's temporary directory and removed with it. The
stream-json output is kept as the transcript and parsed for the final text,
token usage, cost, the MCP servers' status, the tool calls and the retrieval
envelopes the forge tools report. A stream event of the wrong shape is a
parse warning, and a forge tool that reports an error is a tool error: both
make the ask invalid.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AGENT_EFFORT",
    "AGENT_MODEL",
    "GUARD_STOPS",
    "JUDGE_MODEL",
    "ClaudeCall",
    "ClaudeResult",
    "TokenUsage",
    "ask_problems",
    "build_argv",
    "claude_version",
    "mcp_config",
    "parse_stream",
    "run_claude",
]

AGENT_MODEL = "claude-sonnet-5-5"
AGENT_EFFORT = "medium"
JUDGE_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT_S = 600
#: About 28 times Utopia's per-question cost: a guard that should never trip.
MAX_BUDGET_USD = 1.0
#: The only variables a call inherits.
PASSED_ENV = ("PATH", "ANTHROPIC_API_KEY")
FORGE_SERVER = "forge"
FORGE_TOOL_PREFIX = f"mcp__{FORGE_SERVER}__"
#: Result subtypes of a run the CLI stopped at ``--max-budget-usd`` or its turn limit.
# shortcut: names read from the installed CLI's (2.1.319) strings, never seen in a live
# stream; recheck them when the CLI is upgraded.
GUARD_STOPS = frozenset({"error_max_budget_usd", "error_max_turns"})


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        """Lore's token figure: input + output + cache reads + cache writes."""
        return self.input + self.output + self.cache_read + self.cache_write


@dataclass(frozen=True)
class ClaudeCall:
    model: str
    prompt: str
    effort: str | None = None
    #: The ``mcpServers`` mapping; empty for the ``none`` arm and the judge.
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_budget_usd: float = MAX_BUDGET_USD
    claude_bin: str = "claude"


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    usage: TokenUsage
    cost_usd: float
    duration_s: float
    turns: int
    #: ``{server name: status}`` from the stream's init event.
    mcp_servers: dict[str, str]
    tool_calls: tuple[str, ...]
    #: ``{"legs", "vector_leg", "rerank"}`` of every tool result carrying them.
    envelopes: tuple[dict[str, Any], ...]
    #: Set when the run produced no successful result event.
    error: str | None
    guard_tripped: bool
    returncode: int | None
    transcript: Path
    #: The tools the init event names (only ``mcp__forge__*`` may appear).
    tools: tuple[str, ...] = ()
    #: ``mcp__forge__*`` results that are errors.
    tool_errors: tuple[str, ...] = ()
    #: Stream events of the wrong shape.
    warnings: tuple[str, ...] = ()


def mcp_config(mcp_url: str | None, token: str | None) -> dict[str, Any]:
    """``mcpServers`` with only forge's HTTP server (or none, for ``mcp_url=None``)."""
    if mcp_url is None:
        return {}
    return {
        FORGE_SERVER: {
            "type": "http",
            "url": mcp_url,
            "headers": {"Authorization": f"Bearer {token or ''}"},
        }
    }


def build_argv(call: ClaudeCall, mcp_config_path: Path) -> list[str]:
    """The command line; the prompt goes on stdin (never after a variadic flag, never in ps)."""
    argv = [call.claude_bin, "--print", "--output-format", "stream-json"]
    # stream-json under --print requires --verbose.
    argv += ["--verbose", "--model", call.model]
    if call.effort:
        argv += ["--effort", call.effort]
    argv += [
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config_path),
        "--no-session-persistence",
        "--max-budget-usd",
        f"{call.max_budget_usd:.2f}",
    ]
    return argv


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) for block in content if isinstance(block, Mapping)
        )
    return ""


def _envelope(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if isinstance(obj, dict) and "legs" in obj:
        return {key: obj.get(key) for key in ("legs", "vector_leg", "rerank")}
    return None


def _forge_error(text: str) -> bool:
    """Forge's ``isError`` payload: a JSON object holding only ``error``."""
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    return isinstance(obj, dict) and set(obj) == {"error"}


def _need[T](value: object, kind: type[T], what: str) -> T:
    if not isinstance(value, kind):
        raise TypeError(f"{what} is a {type(value).__name__}, not a {kind.__name__}")
    return value


def _apply(event: dict[str, Any], out: dict[str, Any], names: dict[str, str]) -> None:
    """Fold one stream event into ``out``; raises on an event of the wrong shape."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        servers = {}
        for server in _need(event.get("mcp_servers") or [], list, "init mcp_servers"):
            server = _need(server, dict, "an init mcp server")
            servers[str(server.get("name"))] = str(server.get("status"))
        out["mcp_servers"] = servers
        out["tools"] = [str(t) for t in _need(event.get("tools") or [], list, "init tools")]
    elif kind in ("assistant", "user"):
        content = _need(event.get("message"), dict, f"{kind} message").get("content") or []
        if isinstance(content, str):  # plain text, no blocks
            return
        for block in _need(content, list, f"{kind} content"):
            block = _need(block, dict, f"{kind} content block")
            if block.get("type") == "tool_use":
                name = str(block.get("name"))
                out["tool_calls"].append(name)
                names[str(block.get("id"))] = name
            elif block.get("type") == "tool_result":
                text = _text_of(block.get("content"))
                name = names.get(str(block.get("tool_use_id")), "")
                if name.startswith(FORGE_TOOL_PREFIX) and (
                    block.get("is_error") or _forge_error(text)
                ):
                    out["tool_errors"].append(f"{name}: {' '.join(text.split())[:200]}")
                envelope = _envelope(text)
                if envelope is not None:
                    out["envelopes"].append(envelope)
    elif kind == "result":
        usage = _need(event.get("usage") or {}, dict, "result usage")
        out["usage"] = TokenUsage(
            input=int(usage.get("input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
        )
        out["text"] = str(event.get("result") or "")
        out["cost_usd"] = float(event.get("total_cost_usd") or 0.0)
        out["duration_s"] = float(event.get("duration_ms") or 0) / 1000
        out["turns"] = int(event.get("num_turns") or 0)
        subtype = out["subtype"] = event.get("subtype")
        failed = event.get("is_error") or subtype != "success"
        out["error"] = f"result {subtype}" if failed else None


def parse_stream(lines: Iterable[str]) -> dict[str, Any]:
    """The fields of a stream-json run that the harness needs.

    A line that is not JSON is skipped (a killed run ends mid-line); a JSON
    line of the wrong shape is recorded in ``warnings``, never raised.
    """
    out: dict[str, Any] = {
        "text": "",
        "usage": TokenUsage(),
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "turns": 0,
        "mcp_servers": {},
        "tools": [],
        "tool_calls": [],
        "tool_errors": [],
        "envelopes": [],
        "warnings": [],
        "subtype": None,
        "error": "no result event",
    }
    names: dict[str, str] = {}  # tool_use id -> tool name
    for number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        try:
            _apply(_need(event, dict, "the event"), out, names)
        except Exception as exc:  # any wrong shape, whatever it trips over
            out["warnings"].append(f"stream line {number}: {type(exc).__name__}: {exc}")
    return out


def _workdir(root: Path, subject: str) -> None:
    """An empty git repository whose ``origin`` names the subject (no file to read)."""
    root.mkdir()
    for args in (["init", "-q"], ["remote", "add", "origin", f"https://github.com/{subject}"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def run_claude(
    call: ClaudeCall,
    *,
    subject: str,
    scratch: Path,
    env: Mapping[str, str],
    transcript: Path,
) -> ClaudeResult:
    """Run one isolated ``claude --print``; never raises for a failed or slow run."""
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ask-", dir=scratch))
    try:
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        work = tmp / "work"
        _workdir(work, subject)
        config = tmp / "mcp.json"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": dict(call.mcp_servers)}, handle)
        child_env = {key: env[key] for key in PASSED_ENV if key in env}
        child_env["HOME"] = str(home)
        started = time.monotonic()
        guard_tripped, returncode, launch_error = False, None, None
        try:
            proc = subprocess.run(
                build_argv(call, config),
                input=call.prompt,
                cwd=work,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=call.timeout_s,
                check=False,
            )
            stdout, returncode = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:  # run() has killed the process
            guard_tripped = True
            raw = exc.stdout or ""
            stdout = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        except OSError as exc:  # no such binary, not executable
            stdout, launch_error = "", f"cannot run {call.claude_bin}: {exc.strerror}"
        wall = time.monotonic() - started
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(stdout, encoding="utf-8")
    parsed = parse_stream(stdout.splitlines())
    error = launch_error or parsed["error"]
    guard_tripped = guard_tripped or parsed["subtype"] in GUARD_STOPS
    if guard_tripped:
        error = None  # a tripped guard is scored on what it returned, never retried
    elif error and returncode:
        error = f"{error}; exit {returncode}"
    return ClaudeResult(
        text=parsed["text"],
        usage=parsed["usage"],
        cost_usd=parsed["cost_usd"],
        duration_s=parsed["duration_s"] or wall,
        turns=parsed["turns"],
        mcp_servers=parsed["mcp_servers"],
        tool_calls=tuple(parsed["tool_calls"]),
        envelopes=tuple(parsed["envelopes"]),
        error=error,
        guard_tripped=guard_tripped,
        returncode=returncode,
        transcript=transcript,
        tools=tuple(parsed["tools"]),
        tool_errors=tuple(parsed["tool_errors"]),
        warnings=tuple(parsed["warnings"]),
    )


def ask_problems(
    result: ClaudeResult,
    *,
    expect_forge: bool,
    expected_legs: Sequence[str] | None = None,
    expected_rerank: str | None = None,
) -> list[str]:
    """Why an ask is invalid (empty when valid): never scored as a wrong answer.

    The forge arm needs the forge server connected and only forge's tools; the
    ``none`` arm (and the judge) needs no MCP server and no tool at all. A
    forge tool error or a malformed stream event makes the ask invalid. Every
    retrieval envelope must report the configured legs and reranker, and
    never a reranker ``fallback``.
    """
    problems = [result.error] if result.error else []
    problems += [f"parse warning: {warning}" for warning in result.warnings]
    problems += [f"forge tool error: {error}" for error in result.tool_errors]
    stray = [t for t in result.tools if not (expect_forge and t.startswith(FORGE_TOOL_PREFIX))]
    if stray:
        problems.append(f"unexpected tools {stray}")
    servers = result.mcp_servers
    if expect_forge and servers.get(FORGE_SERVER) != "connected":
        problems.append(f"mcp server {FORGE_SERVER} {servers.get(FORGE_SERVER, 'missing')}")
    if not expect_forge and servers:
        problems.append(f"unexpected mcp servers {sorted(servers)}")
    for envelope in result.envelopes:
        if expected_legs is not None and list(envelope.get("legs") or []) != list(expected_legs):
            problems.append(f"retrieval legs {envelope.get('legs')} != {list(expected_legs)}")
        if envelope.get("rerank") == "fallback":
            problems.append("reranker fell back")
        elif expected_rerank is not None and envelope.get("rerank") != expected_rerank:
            problems.append(f"rerank {envelope.get('rerank')} != {expected_rerank}")
    return list(dict.fromkeys(problems))


def claude_version(claude_bin: str = "claude") -> str:
    """``claude --version`` for the run record (empty when it cannot run)."""
    try:
        proc = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()
