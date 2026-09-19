# Local-CLI Model Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Forge drive locally installed agent CLIs (`claude`, `codex`) as a model provider when no BYOK API key is configured, so a self-hoster can evaluate Forge with an existing CLI subscription.

**Architecture:** A pure subprocess `ModelClient` wraps each CLI as a text oracle with its own tool use disabled. Because the host is Darwin/arm64 and the worker container is Linux/aarch64, the worker cannot execute the host binaries; a loopback-only HTTP bridge on the host wraps the subprocess client, and the worker talks to it through a second `ModelClient` implementation. Both satisfy the frozen `forge_contracts.ModelClient` Protocol, so the agent runtime is unchanged.

**Tech Stack:** Python 3.14, Pydantic v2, FastAPI (bridge), `uv` for deps, `pytest`, `ruff`, `mypy`.

**Spec:** `docs/superpowers/specs/2026-09-19-local-cli-provider-design.md`

## Global Constraints

- **Off by default.** `FORGE_LOCAL_CLI_ENABLED` defaults `false`. With it unset, behaviour must be byte-identical to today (503 / scripted fallback).
- **Tool suppression is mandatory.** Every CLI invocation carries flags disabling the CLI's own tool use. `claude`: `--disallowedTools` covering the full tool set plus the most restrictive `--permission-mode`. `codex`: `--sandbox read-only`, never `--dangerously-bypass-approvals-and-sandbox`.
- **Never raise out of `complete()`.** Every failure becomes a `ModelResponse` with a diagnostic `stop_reason` from the fixed set: `provider_unavailable`, `provider_error`, `timeout`, `parse_error`, `auth_error`.
- **Never return silently-empty `tool_calls` on a parse failure.** An empty list must mean "the model chose to do nothing", never "we could not read the answer".
- **Secret hygiene.** `api_key` stays `field(repr=False)`. The bridge never reads or forwards CLI credentials. All CLI stderr passes through the injected `redactor` before it reaches an exception message.
- **Cost is `unpriced`, never zero.** A zero would make local runs look free and corrupt sub-project D.
- **Bridge binds `127.0.0.1` only** unless `FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1`, and refuses to start without `FORGE_CLI_BRIDGE_TOKEN`.
- **Test basenames must be repo-unique.** This repo collects the whole tree with `prepend` import mode; a duplicate basename fails collection with exit 2 across unrelated jobs. Verify before each commit with:
  `find . -name 'test_*.py' -not -path '*/.venv/*' | xargs -n1 basename | sort | uniq -d`
- **Keyword-only constructors**, matching `AnthropicModelClient` and `OpenAIModelClient`.
- Run tests with `uv run pytest`, lint with `make lint`, types with `make typecheck`.

---

### Task 1: CLI detection

**Files:**
- Create: `packages/agent-runtime/forge_agent/providers/local_cli/__init__.py`
- Create: `packages/agent-runtime/forge_agent/providers/local_cli/detect.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_detect.py`

**Interfaces:**
- Consumes: nothing
- Produces: `CliName` (`StrEnum` with members `claude`, `codex`), `DetectedCli` (frozen dataclass: `name: CliName`, `path: str`), `detect_clis(*, preference: Sequence[CliName] | None = None, which: Callable[[str], str | None] = shutil.which) -> list[DetectedCli]`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_detect.py
from forge_agent.providers.local_cli.detect import CliName, detect_clis


def _which(found: dict[str, str]):
    return lambda name: found.get(name)


def test_detects_both_in_preference_order():
    found = _which({"claude": "/usr/local/bin/claude", "codex": "/usr/local/bin/codex"})
    result = detect_clis(preference=[CliName.codex, CliName.claude], which=found)
    assert [d.name for d in result] == [CliName.codex, CliName.claude]
    assert result[0].path == "/usr/local/bin/codex"


def test_returns_empty_when_nothing_installed():
    assert detect_clis(which=_which({})) == []


def test_never_raises_when_which_explodes():
    def boom(_name: str) -> str | None:
        raise OSError("PATH is on fire")

    assert detect_clis(which=boom) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_detect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge_agent.providers.local_cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# packages/agent-runtime/forge_agent/providers/local_cli/detect.py
"""Discover locally installed agent CLIs. Never raises; absence is not an error."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["CliName", "DetectedCli", "DEFAULT_PREFERENCE", "detect_clis"]


class CliName(StrEnum):
    claude = "claude"
    codex = "codex"


#: Order tried when the operator expresses no preference.
DEFAULT_PREFERENCE: tuple[CliName, ...] = (CliName.claude, CliName.codex)


@dataclass(frozen=True)
class DetectedCli:
    name: CliName
    path: str


def detect_clis(
    *,
    preference: Sequence[CliName] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[DetectedCli]:
    """Return installed CLIs in ``preference`` order. Empty on any failure."""
    order = tuple(preference) if preference else DEFAULT_PREFERENCE
    found: list[DetectedCli] = []
    for name in order:
        try:
            path = which(str(name))
        except Exception:  # noqa: BLE001 - detection must never break resolution
            continue
        if path:
            found.append(DetectedCli(name=name, path=path))
    return found
```

Create `local_cli/__init__.py` re-exporting `CliName`, `DetectedCli`, `DEFAULT_PREFERENCE`, `detect_clis`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_detect.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
find . -name 'test_*.py' -not -path '*/.venv/*' | xargs -n1 basename | sort | uniq -d
git add packages/agent-runtime/forge_agent/providers/local_cli/ packages/agent-runtime/tests/providers/test_local_cli_detect.py
git commit -m "feat(providers): detect locally installed agent CLIs"
```

---

### Task 2: Provider config and auto-detect resolution

**Files:**
- Modify: `packages/agent-runtime/forge_agent/providers/config.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_config.py`

**Interfaces:**
- Consumes: `CliName`, `detect_clis` from Task 1
- Produces: `ProviderName.local_cli`; `ModelClientConfig.api_key` now defaults `""`; `ModelClientConfig.cli_name: CliName | None = None`; `ModelClientConfig.bridge_url: str | None = None`; `ModelClientConfig.bridge_token: str = field(repr=False, default="")`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_config.py
from forge_agent.providers.config import ModelClientConfig, ProviderName
from forge_agent.providers.local_cli.detect import CliName, DetectedCli


def _detect_claude(**_kw):
    return [DetectedCli(name=CliName.claude, path="/usr/local/bin/claude")]


def _detect_none(**_kw):
    return []


def test_no_optin_means_no_config():
    """Byte-identical to today: absent opt-in, resolution still returns None."""
    assert ModelClientConfig.from_env({}, detect=_detect_claude) is None


def test_explicit_provider_still_wins_over_local_cli():
    env = {
        "FORGE_MODEL_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-test",
        "FORGE_LOCAL_CLI_ENABLED": "1",
    }
    config = ModelClientConfig.from_env(env, detect=_detect_claude)
    assert config is not None
    assert config.provider is ProviderName.anthropic


def test_local_cli_used_when_optin_and_cli_present():
    config = ModelClientConfig.from_env(
        {"FORGE_LOCAL_CLI_ENABLED": "1"}, detect=_detect_claude
    )
    assert config is not None
    assert config.provider is ProviderName.local_cli
    assert config.cli_name is CliName.claude
    assert config.api_key == ""


def test_optin_but_no_cli_returns_none():
    assert (
        ModelClientConfig.from_env({"FORGE_LOCAL_CLI_ENABLED": "1"}, detect=_detect_none)
        is None
    )


def test_bridge_url_selects_local_cli_without_local_binary():
    config = ModelClientConfig.from_env(
        {
            "FORGE_LOCAL_CLI_ENABLED": "1",
            "FORGE_CLI_BRIDGE_URL": "http://host.docker.internal:8099",
            "FORGE_CLI_BRIDGE_TOKEN": "t0ken",
        },
        detect=_detect_none,
    )
    assert config is not None
    assert config.provider is ProviderName.local_cli
    assert config.bridge_url == "http://host.docker.internal:8099"


def test_api_key_never_in_repr():
    config = ModelClientConfig.from_env(
        {"FORGE_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-secret"}
    )
    assert "sk-secret" not in repr(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_config.py -v`
Expected: FAIL — `from_env() got an unexpected keyword argument 'detect'`

- [ ] **Step 3: Write minimal implementation**

In `config.py`, add the enum member and fields, then insert the local-CLI branch into `from_env`.

```python
class ProviderName(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    local_cli = "local_cli"
```

Add to `_DEFAULT_MODEL`: `ProviderName.local_cli: "local-cli"`. Add to `_KEY_ENV`: `ProviderName.local_cli: "FORGE_LOCAL_CLI_UNUSED"` (never read; present so the dict stays total).

Change the dataclass field and add three new ones:

```python
    api_key: str = field(repr=False, default="")
    cli_name: "CliName | None" = None
    bridge_url: str | None = None
    bridge_token: str = field(repr=False, default="")
```

Replace the `from_env` signature and add the fallback branch after the existing `if not raw_provider: return None` check:

```python
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        detect: "Callable[..., list[DetectedCli]]" = detect_clis,
    ) -> "ModelClientConfig | None":
        source = os.environ if env is None else env
        raw_provider = (source.get("FORGE_MODEL_PROVIDER") or "").strip().lower()
        if not raw_provider:
            return cls._from_local_cli(source, detect)
        # ... existing body unchanged ...
```

```python
    @classmethod
    def _from_local_cli(
        cls,
        source: Mapping[str, str],
        detect: "Callable[..., list[DetectedCli]]",
    ) -> "ModelClientConfig | None":
        """Resolve a local-CLI config, or ``None`` to keep today's fallback."""
        if not _bool(source.get("FORGE_LOCAL_CLI_ENABLED"), default=False):
            return None

        bridge_url = (source.get("FORGE_CLI_BRIDGE_URL") or "").strip() or None
        preference = _preference(source.get("FORGE_LOCAL_CLI_PREFERENCE"))

        cli_name: CliName | None = None
        if bridge_url is None:
            detected = detect(preference=preference)
            if not detected:
                return None
            cli_name = detected[0].name
        elif preference:
            cli_name = preference[0]

        return cls(
            provider=ProviderName.local_cli,
            model=str(cli_name) if cli_name else "local-cli",
            api_key="",
            cli_name=cli_name,
            bridge_url=bridge_url,
            bridge_token=(source.get("FORGE_CLI_BRIDGE_TOKEN") or "").strip(),
            timeout_s=_float(source.get("FORGE_LOCAL_CLI_TIMEOUT_S"), 600.0),
        )


def _preference(raw: str | None) -> list[CliName] | None:
    if not raw or not raw.strip():
        return None
    names: list[CliName] = []
    for part in raw.split(","):
        try:
            names.append(CliName(part.strip().lower()))
        except ValueError:
            continue
    return names or None
```

Import at the top of `config.py`:

```python
from collections.abc import Callable, Mapping
from forge_agent.providers.local_cli.detect import CliName, DetectedCli, detect_clis
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_config.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Verify no existing behaviour changed**

Run: `uv run pytest packages/agent-runtime/tests/providers -v`
Expected: PASS — every pre-existing provider test still green. If any fails, the `api_key` default or the `from_env` early return has changed existing semantics; fix before committing.

- [ ] **Step 6: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/config.py packages/agent-runtime/tests/providers/test_local_cli_config.py
git commit -m "feat(providers): local_cli provider name and auto-detect resolution"
```

---

### Task 3: Envelope translation (pure)

**Files:**
- Modify: `packages/agent-runtime/forge_agent/providers/translate.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_translate.py`

**Interfaces:**
- Consumes: `ModelRequest`, `ModelResponse`, `ModelToolCall`, `TokenUsage` from `forge_contracts`
- Produces: `build_local_cli_prompt(request: ModelRequest) -> tuple[str, str]` returning `(system, user)`; `local_cli_output_schema() -> dict[str, Any]`; `parse_local_cli_response(raw: str) -> ModelResponse`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_translate.py
import pytest

from forge_agent.providers.translate import (
    build_local_cli_prompt,
    local_cli_output_schema,
    parse_local_cli_response,
)
from forge_contracts import ModelMessage, ModelRequest


def _request() -> ModelRequest:
    return ModelRequest(
        model="claude",
        system="You are terse.",
        messages=[ModelMessage(role="user", content="Read app/main.py")],
        tools=[{"name": "read_file", "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    )


def test_prompt_carries_tool_schemas_and_envelope_instruction():
    system, user = build_local_cli_prompt(_request())
    assert "You are terse." in system
    assert "read_file" in system
    assert "tool_calls" in system
    assert "Read app/main.py" in user


def test_parses_tool_calls():
    raw = '{"content": "reading", "tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}], "stop_reason": "tool_use"}'
    response = parse_local_cli_response(raw)
    assert response.content == "reading"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.stop_reason == "tool_use"


def test_parses_envelope_wrapped_in_prose():
    """CLIs sometimes surround JSON with commentary; extract the object."""
    raw = 'Sure!\n```json\n{"content": "hi", "tool_calls": [], "stop_reason": "end_turn"}\n```\nDone.'
    response = parse_local_cli_response(raw)
    assert response.content == "hi"
    assert response.stop_reason == "end_turn"


@pytest.mark.parametrize("raw", ["", "not json at all", "{unclosed", "[]"])
def test_unparseable_yields_parse_error_not_empty_tool_calls(raw):
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_output_schema_is_a_json_schema_object():
    schema = local_cli_output_schema()
    assert schema["type"] == "object"
    assert "tool_calls" in schema["properties"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_translate.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_local_cli_prompt'`

- [ ] **Step 3: Write minimal implementation**

Append to `translate.py`:

```python
_ENVELOPE_INSTRUCTION = """
Reply with a single JSON object and nothing else:

{"content": "<prose, may be empty>",
 "tool_calls": [{"name": "<tool name>", "arguments": {<json object>}}],
 "stop_reason": "tool_use" | "end_turn"}

Use "tool_calls" only for the tools listed below. Emit an empty list when you
have no tool to call. Do not invoke any tool yourself; only describe the call.
"""


def local_cli_output_schema() -> dict[str, Any]:
    """JSON Schema for the envelope, for CLIs that can enforce one."""
    return {
        "type": "object",
        "required": ["content", "tool_calls", "stop_reason"],
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string"},
            "stop_reason": {"type": "string", "enum": ["tool_use", "end_turn"]},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                },
            },
        },
    }


def build_local_cli_prompt(request: ModelRequest) -> tuple[str, str]:
    """Return ``(system, user)`` for a CLI invocation."""
    parts: list[str] = []
    if request.system:
        parts.append(request.system)
    parts.append(_ENVELOPE_INSTRUCTION.strip())
    if request.tools:
        parts.append("Available tools:\n" + json.dumps(request.tools, indent=2))
    system = "\n\n".join(parts)
    user = "\n\n".join(m.content for m in request.messages if m.content)
    return system, user


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Find the outermost JSON object in ``raw``; tolerate surrounding prose."""
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(raw[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def parse_local_cli_response(raw: str) -> ModelResponse:
    """Fold a CLI envelope into a ``ModelResponse``.

    Unparseable output yields ``stop_reason="parse_error"`` — never an empty
    ``tool_calls`` list, which would be indistinguishable from the model
    deliberately choosing to do nothing.
    """
    payload = _extract_json_object(raw)
    if payload is None:
        return ModelResponse(content="", stop_reason="parse_error")

    calls: list[ModelToolCall] = []
    for entry in payload.get("tool_calls") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            return ModelResponse(content="", stop_reason="parse_error")
        arguments = entry.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ModelResponse(content="", stop_reason="parse_error")
        calls.append(ModelToolCall(name=str(entry["name"]), arguments=arguments))

    usage = payload.get("usage") or {}
    return ModelResponse(
        content=str(payload.get("content") or ""),
        tool_calls=calls,
        stop_reason=str(payload.get("stop_reason") or "end_turn"),
        usage=TokenUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
        if usage
        else None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_translate.py -v`
Expected: PASS (8 passed, counting the parametrised cases)

- [ ] **Step 5: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/translate.py packages/agent-runtime/tests/providers/test_local_cli_translate.py
git commit -m "feat(providers): local-CLI envelope translation with strict parsing"
```

---

### Task 4: CLI backends

**Files:**
- Create: `packages/agent-runtime/forge_agent/providers/local_cli/backends.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_backends.py`

**Interfaces:**
- Consumes: `CliName` (Task 1), `local_cli_output_schema` (Task 3)
- Produces: `CliResult` (frozen dataclass: `stdout: str`, `stderr: str`, `returncode: int`, `timed_out: bool`); `CliBackend` Protocol with `argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]` and `needs_schema_file: bool`; `ClaudeCliBackend`, `CodexCliBackend`; `backend_for(name: CliName) -> CliBackend`; `run_cli(argv: list[str], *, timeout_s: float, runner=subprocess.run) -> CliResult`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_backends.py
import subprocess

import pytest

from forge_agent.providers.local_cli.backends import backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName


def test_claude_argv_disables_tools_and_prints():
    argv = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
    assert "--print" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--disallowedTools" in argv
    assert "--permission-mode" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_codex_argv_is_read_only_and_schema_bound():
    argv = backend_for(CliName.codex).argv(system="S", user="U", schema_path="/tmp/s.json")
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "read-only" in argv
    assert "--output-schema" in argv and "/tmp/s.json" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_declares_it_needs_a_schema_file():
    assert backend_for(CliName.codex).needs_schema_file is True
    assert backend_for(CliName.claude).needs_schema_file is False


def test_run_cli_captures_output():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="err")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.stdout == "out"
    assert result.returncode == 0
    assert result.timed_out is False


def test_run_cli_marks_timeout_rather_than_raising():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.timed_out is True
    assert result.returncode != 0


def test_run_cli_survives_missing_binary():
    def runner(argv, **kwargs):
        raise FileNotFoundError("no such binary")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.returncode != 0
    assert result.timed_out is False


@pytest.mark.parametrize("name", list(CliName))
def test_every_backend_suppresses_tools(name):
    """Global constraint: no backend may ever omit tool suppression."""
    argv = backend_for(name).argv(system="S", user="U", schema_path="/tmp/s.json")
    joined = " ".join(argv)
    assert ("--disallowedTools" in joined) or ("--sandbox read-only" in joined.replace("  ", " "))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_backends.py -v`
Expected: FAIL — `ModuleNotFoundError: ...local_cli.backends`

- [ ] **Step 3: Write minimal implementation**

```python
# packages/agent-runtime/forge_agent/providers/local_cli/backends.py
"""Argv construction and subprocess execution for each supported agent CLI.

Every backend disables the CLI's own tool use. The CLIs are agents; left
unconstrained they would plan and edit files on the host while Forge's own loop
also acted — two agent loops, one working tree, no sandbox. Here they are text
oracles only; all tool execution stays inside Forge's audited sandbox.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from forge_agent.providers.local_cli.detect import CliName

__all__ = ["CliBackend", "CliResult", "ClaudeCliBackend", "CodexCliBackend", "backend_for", "run_cli"]

#: Claude Code tool names suppressed on every invocation.
_CLAUDE_TOOLS = (
    "Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)


@dataclass(frozen=True)
class CliResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class CliBackend(Protocol):
    needs_schema_file: bool

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]: ...


class ClaudeCliBackend:
    """`claude -p` with a JSON result and every tool disabled."""

    needs_schema_file = False

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]:
        return [
            "claude",
            "--print",
            "--output-format", "json",
            "--permission-mode", "plan",
            "--disallowedTools", _CLAUDE_TOOLS,
            "--append-system-prompt", system,
            user,
        ]


class CodexCliBackend:
    """`codex exec` with a read-only sandbox and a schema-enforced result."""

    needs_schema_file = True

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]:
        argv = ["codex", "exec", "--json", "--sandbox", "read-only"]
        if schema_path:
            argv += ["--output-schema", schema_path]
        argv.append(f"{system}\n\n{user}")
        return argv


_BACKENDS: dict[CliName, CliBackend] = {
    CliName.claude: ClaudeCliBackend(),
    CliName.codex: CodexCliBackend(),
}


def backend_for(name: CliName) -> CliBackend:
    return _BACKENDS[name]


def run_cli(
    argv: list[str],
    *,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CliResult:
    """Execute ``argv``. Never raises; failures come back as a ``CliResult``."""
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return CliResult(stdout="", stderr="timed out", returncode=124, timed_out=True)
    except (FileNotFoundError, OSError) as exc:
        return CliResult(stdout="", stderr=str(exc), returncode=127, timed_out=False)
    return CliResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_backends.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Verify the real CLI accepts these flags**

Run: `claude --print --output-format json --permission-mode plan --disallowedTools "Bash,Edit" "say ok"`
Expected: a JSON object on stdout. If `--permission-mode plan` is rejected, substitute the most restrictive mode `claude --help` lists and update `ClaudeCliBackend` plus the test.

Run: `codex exec --json --sandbox read-only "say ok"`
Expected: JSON events on stdout. If rejected, adjust `CodexCliBackend` and its test.

- [ ] **Step 6: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/local_cli/backends.py packages/agent-runtime/tests/providers/test_local_cli_backends.py
git commit -m "feat(providers): claude and codex CLI backends with mandatory tool suppression"
```

---

### Task 5: LocalCliModelClient

**Files:**
- Create: `packages/agent-runtime/forge_agent/providers/local_cli/client.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_client.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4
- Produces: `LocalCliModelClient(*, cli_name: CliName, timeout_s: float = 600.0, redactor: Callable[[str], str] = _identity, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run)` implementing `complete(ModelRequest) -> ModelResponse` and `stream(ModelRequest) -> Iterator[ModelStreamEvent]`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_client.py
import subprocess

from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName
from forge_contracts import ModelMessage, ModelRequest

ENVELOPE = '{"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}'


def _request() -> ModelRequest:
    return ModelRequest(model="claude", messages=[ModelMessage(role="user", content="hi")])


def _runner(stdout: str, returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_successful_completion():
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(ENVELOPE))
    response = client.complete(_request())
    assert response.content == "ok"
    assert response.stop_reason == "end_turn"


def test_claude_json_wrapper_is_unwrapped():
    wrapper = '{"type":"result","result":%s}' % ENVELOPE
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(wrapper))
    assert client.complete(_request()).content == "ok"


def test_nonzero_exit_is_provider_error():
    client = LocalCliModelClient(
        cli_name=CliName.claude, runner=_runner("", returncode=2, stderr="boom")
    )
    assert client.complete(_request()).stop_reason == "provider_error"


def test_missing_binary_is_provider_unavailable():
    def run(argv, **kwargs):
        raise FileNotFoundError("nope")

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run)
    assert client.complete(_request()).stop_reason == "provider_unavailable"


def test_timeout_is_reported_as_timeout():
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run, timeout_s=1.0)
    assert client.complete(_request()).stop_reason == "timeout"


def test_auth_failure_detected_from_stderr():
    client = LocalCliModelClient(
        cli_name=CliName.claude,
        runner=_runner("", returncode=1, stderr="Please run `claude login` to authenticate"),
    )
    assert client.complete(_request()).stop_reason == "auth_error"


def test_garbage_retries_once_then_reports_parse_error():
    run = _runner("not json")
    client = LocalCliModelClient(cli_name=CliName.claude, runner=run)
    response = client.complete(_request())
    assert response.stop_reason == "parse_error"
    assert len(run.calls) == 2, "must attempt exactly one repair retry"


def test_repair_retry_can_succeed():
    outputs = iter(["garbage", ENVELOPE])

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=next(outputs), stderr="")

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run)
    assert client.complete(_request()).content == "ok"


def test_stderr_passes_through_redactor():
    seen: list[str] = []

    def redactor(value: str) -> str:
        seen.append(value)
        return "[scrubbed]"

    client = LocalCliModelClient(
        cli_name=CliName.claude,
        runner=_runner("", returncode=2, stderr="key sk-abc123"),
        redactor=redactor,
    )
    response = client.complete(_request())
    assert "sk-abc123" not in (response.content or "")
    assert seen, "redactor must be consulted on failure"


def test_stream_yields_one_chunk():
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(ENVELOPE))
    events = list(client.stream(_request()))
    assert len(events) == 1
    assert events[0].text == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_client.py -v`
Expected: FAIL — `ModuleNotFoundError: ...local_cli.client`

- [ ] **Step 3: Write minimal implementation**

```python
# packages/agent-runtime/forge_agent/providers/local_cli/client.py
"""A ``ModelClient`` that drives a locally installed agent CLI as a text oracle.

Never raises out of ``complete``: every failure is a ``ModelResponse`` carrying a
diagnostic ``stop_reason``, because the agent loop distinguishes "did nothing" from
"could not be read" only by that field.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator

from forge_agent.providers import translate
from forge_agent.providers.local_cli.backends import CliResult, backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["LocalCliModelClient"]

_logger = logging.getLogger("forge_agent.providers.local_cli")

_REPAIR = (
    "Your previous reply was not the required JSON object. "
    "Reply again with the JSON object only, no prose and no code fence."
)

_AUTH_MARKERS = ("login", "unauthenticated", "not authenticated", "authentication")


def _identity(value: str) -> str:
    return value


class LocalCliModelClient:
    """A ``ModelClient`` backed by a local agent CLI subprocess."""

    def __init__(
        self,
        *,
        cli_name: CliName,
        timeout_s: float = 600.0,
        redactor: Callable[[str], str] = _identity,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._cli_name = cli_name
        self._backend = backend_for(cli_name)
        self._timeout_s = timeout_s
        self._redactor = redactor
        self._runner = runner

    def complete(self, request: ModelRequest) -> ModelResponse:
        system, user = translate.build_local_cli_prompt(request)
        result = self._invoke(system, user)
        failure = self._failure_for(result)
        if failure is not None:
            return failure

        response = translate.parse_local_cli_response(self._unwrap(result.stdout))
        if response.stop_reason != "parse_error":
            return response

        _logger.warning(
            "local cli %s returned unparseable output; retrying once", self._cli_name
        )
        retry = self._invoke(f"{system}\n\n{_REPAIR}", user)
        failure = self._failure_for(retry)
        if failure is not None:
            return failure
        return translate.parse_local_cli_response(self._unwrap(retry.stdout))

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)

    # -- internals ---------------------------------------------------------

    def _invoke(self, system: str, user: str) -> CliResult:
        schema_path: str | None = None
        handle: int | None = None
        try:
            if self._backend.needs_schema_file:
                handle, schema_path = tempfile.mkstemp(suffix=".json", prefix="forge-cli-")
                with os.fdopen(handle, "w") as fh:
                    json.dump(translate.local_cli_output_schema(), fh)
                handle = None
            argv = self._backend.argv(system=system, user=user, schema_path=schema_path)
            return run_cli(argv, timeout_s=self._timeout_s, runner=self._runner)
        finally:
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

    def _failure_for(self, result: CliResult) -> ModelResponse | None:
        if result.timed_out:
            return ModelResponse(content="", stop_reason="timeout")
        if result.returncode == 127:
            return ModelResponse(content="", stop_reason="provider_unavailable")
        if result.returncode != 0:
            scrubbed = self._redactor(result.stderr or "")
            lowered = (result.stderr or "").lower()
            reason = (
                "auth_error"
                if any(marker in lowered for marker in _AUTH_MARKERS)
                else "provider_error"
            )
            _logger.warning("local cli %s failed (%s): %s", self._cli_name, reason, scrubbed)
            return ModelResponse(content="", stop_reason=reason)
        return None

    @staticmethod
    def _unwrap(stdout: str) -> str:
        """Unwrap a CLI result wrapper so the inner envelope can be parsed."""
        try:
            outer = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return stdout
        if isinstance(outer, dict) and "result" in outer:
            inner = outer["result"]
            return inner if isinstance(inner, str) else json.dumps(inner)
        return stdout
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_client.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/local_cli/client.py packages/agent-runtime/tests/providers/test_local_cli_client.py
git commit -m "feat(providers): LocalCliModelClient with repair retry and diagnostic stop reasons"
```

---

### Task 6: Factory wiring

**Files:**
- Modify: `packages/agent-runtime/forge_agent/providers/base.py`
- Modify: `packages/agent-runtime/forge_agent/providers/__init__.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_factory.py`

**Interfaces:**
- Consumes: Tasks 2, 5
- Produces: `build_model_client` returns a `LocalCliModelClient` for `ProviderName.local_cli`; `LocalCliModelClient` and `CliName` exported from `forge_agent.providers`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_factory.py
import pytest

from forge_agent.providers import ModelClientConfig, ProviderName, build_model_client
from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName


def test_factory_builds_local_cli_client():
    config = ModelClientConfig(
        provider=ProviderName.local_cli, model="claude", cli_name=CliName.claude
    )
    assert isinstance(build_model_client(config), LocalCliModelClient)


def test_factory_rejects_local_cli_without_a_cli_name():
    from forge_agent.providers.base import ModelClientError

    config = ModelClientConfig(provider=ProviderName.local_cli, model="local-cli")
    with pytest.raises(ModelClientError):
        build_model_client(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_factory.py -v`
Expected: FAIL — `ModelClientError: unknown model provider: <ProviderName.local_cli: 'local_cli'>`

- [ ] **Step 3: Write minimal implementation**

In `base.py`, before the final `raise`:

```python
    if provider is ProviderName.local_cli:
        from forge_agent.providers.local_cli.client import LocalCliModelClient

        if config.bridge_url:
            from forge_agent.providers.bridge_client import LocalCliBridgeClient

            return LocalCliBridgeClient(
                base_url=config.bridge_url,
                token=config.bridge_token,
                timeout_s=config.timeout_s,
                redactor=redactor,
            )
        if config.cli_name is None:
            raise ModelClientError("local_cli provider requires a cli_name or bridge_url")
        return LocalCliModelClient(
            cli_name=config.cli_name,
            timeout_s=config.timeout_s,
            redactor=redactor,
            runner=client if client is not None else subprocess.run,
        )
```

Add `import subprocess` at the top of `base.py`.

In `providers/__init__.py`, add the imports and `__all__` entries for `LocalCliModelClient`, `LocalCliBridgeClient`, `CliName`, `detect_clis`.

> Note: `LocalCliBridgeClient` is created in Task 8. Implement Task 8 before running this task's full suite, or temporarily guard the bridge branch. The recommended order is 6 → 8 in one sitting.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_factory.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Verify nothing else regressed**

Run: `uv run pytest packages/agent-runtime -q`
Expected: PASS, no failures

- [ ] **Step 6: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/base.py packages/agent-runtime/forge_agent/providers/__init__.py packages/agent-runtime/tests/providers/test_local_cli_factory.py
git commit -m "feat(providers): wire local_cli into the model client factory"
```

---

### Task 7: The host bridge app

**Files:**
- Create: `apps/cli-bridge/forge_cli_bridge/__init__.py`
- Create: `apps/cli-bridge/forge_cli_bridge/app.py`
- Create: `apps/cli-bridge/forge_cli_bridge/settings.py`
- Create: `apps/cli-bridge/pyproject.toml`
- Create: `apps/cli-bridge/README.md`
- Test: `apps/cli-bridge/tests/test_cli_bridge_app.py`

**Interfaces:**
- Consumes: Tasks 1, 5
- Produces: `create_app(settings: BridgeSettings) -> FastAPI` exposing `GET /health` and `POST /v1/complete`; `BridgeSettings(token: str, host: str = "127.0.0.1", port: int = 8099, allow_non_loopback: bool = False, timeout_s: float = 600.0)`

- [ ] **Step 1: Write the failing test**

```python
# apps/cli-bridge/tests/test_cli_bridge_app.py
import pytest
from fastapi.testclient import TestClient

from forge_cli_bridge.app import create_app
from forge_cli_bridge.settings import BridgeSettings

ENVELOPE = '{"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}'


def _client(**overrides):
    settings = BridgeSettings(token="t0ken", **overrides)
    return TestClient(create_app(settings))


def _body():
    return {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}


def test_health_reports_detected_clis():
    response = _client().get("/health")
    assert response.status_code == 200
    assert "clis" in response.json()


def test_complete_requires_a_token():
    assert _client().post("/v1/complete", json=_body()).status_code == 401


def test_complete_rejects_a_wrong_token():
    response = _client().post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_settings_refuse_an_empty_token():
    with pytest.raises(ValueError):
        BridgeSettings(token="")


def test_settings_refuse_non_loopback_without_override():
    with pytest.raises(ValueError):
        BridgeSettings(token="t0ken", host="0.0.0.0")


def test_settings_allow_non_loopback_with_override():
    settings = BridgeSettings(token="t0ken", host="0.0.0.0", allow_non_loopback=True)
    assert settings.host == "0.0.0.0"


def test_complete_returns_a_model_response(monkeypatch):
    import subprocess

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    app_client = _client()
    app_client.app.state.runner = runner
    response = app_client.post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer t0ken"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/cli-bridge/tests/test_cli_bridge_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'forge_cli_bridge'`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/cli-bridge/forge_cli_bridge/settings.py
"""Bridge configuration. Refuses unsafe combinations at construction time."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BridgeSettings"]

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class BridgeSettings:
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8099
    allow_non_loopback: bool = False
    timeout_s: float = 600.0

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError(
                "FORGE_CLI_BRIDGE_TOKEN is required; the bridge executes host "
                "binaries and will not start unauthenticated"
            )
        if self.host not in _LOOPBACK and not self.allow_non_loopback:
            raise ValueError(
                f"refusing to bind {self.host!r}: set "
                "FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1 to override"
            )
```

```python
# apps/cli-bridge/forge_cli_bridge/app.py
"""Loopback HTTP bridge exposing the host's agent CLIs to a containerised worker.

The worker runs Linux/aarch64; the host CLIs are Mach-O/arm64 binaries and cannot
execute inside the container. This bridge runs on the host, so CLI credentials
never enter a container and no Linux CLI build is required.

Development and self-host convenience only. Not for shared or production use.
"""

from __future__ import annotations

import logging
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException
from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName, detect_clis
from forge_cli_bridge.settings import BridgeSettings
from forge_contracts import ModelRequest, ModelResponse

__all__ = ["create_app"]

_logger = logging.getLogger("forge_cli_bridge")


def create_app(settings: BridgeSettings) -> FastAPI:
    app = FastAPI(title="Forge CLI bridge", version="0.1.0")
    app.state.settings = settings
    app.state.runner = subprocess.run

    def _authorize(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {settings.token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing bridge token")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "clis": [d.name.value for d in detect_clis()],
            "loopback_only": not settings.allow_non_loopback,
        }

    @app.post("/v1/complete", dependencies=[Depends(_authorize)])
    def complete(request: ModelRequest) -> ModelResponse:
        detected = detect_clis()
        if not detected:
            return ModelResponse(content="", stop_reason="provider_unavailable")
        client = LocalCliModelClient(
            cli_name=CliName(detected[0].name),
            timeout_s=settings.timeout_s,
            runner=app.state.runner,
        )
        return client.complete(request)

    if settings.allow_non_loopback:
        _logger.warning(
            "CLI bridge is bound to %s — it executes host binaries; do not expose it",
            settings.host,
        )
    return app
```

`pyproject.toml` mirrors `apps/mcp-gateway/pyproject.toml`, depending on `forge-agent` and `forge-contracts`, and registers the package in the workspace. `README.md` states plainly that the bridge is for development and self-host use only, never a shared deployment.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/cli-bridge/tests/test_cli_bridge_app.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
find . -name 'test_*.py' -not -path '*/.venv/*' | xargs -n1 basename | sort | uniq -d
git add apps/cli-bridge/
git commit -m "feat(cli-bridge): loopback host bridge exposing local agent CLIs"
```

---

### Task 8: The bridge client

**Files:**
- Create: `packages/agent-runtime/forge_agent/providers/bridge_client.py`
- Test: `packages/agent-runtime/tests/providers/test_local_cli_bridge_client.py`

**Interfaces:**
- Consumes: Task 7's HTTP contract
- Produces: `LocalCliBridgeClient(*, base_url: str, token: str, timeout_s: float = 600.0, redactor: Callable[[str], str] = _identity, transport: Any | None = None)` implementing `ModelClient`

- [ ] **Step 1: Write the failing test**

```python
# packages/agent-runtime/tests/providers/test_local_cli_bridge_client.py
import httpx

from forge_agent.providers.bridge_client import LocalCliBridgeClient
from forge_contracts import ModelMessage, ModelRequest


def _request() -> ModelRequest:
    return ModelRequest(model="claude", messages=[ModelMessage(role="user", content="hi")])


def _client(handler) -> LocalCliBridgeClient:
    return LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        transport=httpx.MockTransport(handler),
    )


def test_successful_round_trip_and_token_header():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"content": "ok", "tool_calls": [], "stop_reason": "end_turn"})

    assert _client(handler).complete(_request()).content == "ok"
    assert seen["auth"] == "Bearer t0ken"


def test_connection_refused_is_provider_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _client(handler).complete(_request()).stop_reason == "provider_unavailable"


def test_401_is_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    assert _client(handler).complete(_request()).stop_reason == "auth_error"


def test_500_is_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert _client(handler).complete(_request()).stop_reason == "provider_error"


def test_timeout_is_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _client(handler).complete(_request()).stop_reason == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_bridge_client.py -v`
Expected: FAIL — `ModuleNotFoundError: ...providers.bridge_client`

- [ ] **Step 3: Write minimal implementation**

```python
# packages/agent-runtime/forge_agent/providers/bridge_client.py
"""A ``ModelClient`` that reaches local agent CLIs through the host bridge.

Maps every transport failure onto the same ``stop_reason`` vocabulary the
subprocess client uses, so the agent runtime cannot tell which transport served
a request — and does not need to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["LocalCliBridgeClient"]

_logger = logging.getLogger("forge_agent.providers.bridge")


def _identity(value: str) -> str:
    return value


class LocalCliBridgeClient:
    """A ``ModelClient`` speaking HTTP to a host-side CLI bridge."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_s: float = 600.0,
        redactor: Callable[[str], str] = _identity,
        transport: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._redactor = redactor
        self._transport = transport

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.post(
                    f"{self._base_url}/v1/complete",
                    json=request.model_dump(mode="json"),
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ModelResponse(content="", stop_reason="provider_unavailable")
        except httpx.TimeoutException:
            return ModelResponse(content="", stop_reason="timeout")
        except httpx.HTTPError as exc:
            _logger.warning("cli bridge transport error: %s", self._redactor(str(exc)))
            return ModelResponse(content="", stop_reason="provider_error")

        if response.status_code in (401, 403):
            return ModelResponse(content="", stop_reason="auth_error")
        if response.status_code >= 400:
            _logger.warning("cli bridge returned %s", response.status_code)
            return ModelResponse(content="", stop_reason="provider_error")

        try:
            return ModelResponse.model_validate(response.json())
        except Exception:  # noqa: BLE001 - a malformed bridge reply is a parse failure
            return ModelResponse(content="", stop_reason="parse_error")

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/agent-runtime/tests/providers/test_local_cli_bridge_client.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the whole provider suite**

Run: `uv run pytest packages/agent-runtime/tests/providers -q`
Expected: PASS — Task 6's factory test now resolves the bridge import

- [ ] **Step 6: Commit**

```bash
git add packages/agent-runtime/forge_agent/providers/bridge_client.py packages/agent-runtime/tests/providers/test_local_cli_bridge_client.py
git commit -m "feat(providers): HTTP bridge client for host-side local CLIs"
```

---

### Task 9: Unpriced cost marking

**Files:**
- Modify: `packages/observability/forge_obs/cost/meter.py`
- Modify: `packages/observability/forge_obs/cost/pricing.py`
- Test: `packages/observability/tests/test_cost_unpriced_local_cli.py`

**Interfaces:**
- Consumes: Task 2's `ProviderName.local_cli`
- Produces: cost records for local-CLI runs carry `unpriced=True` and `cost_usd=None`

- [ ] **Step 1: Read the existing meter first**

Run: `sed -n '1,60p' packages/observability/forge_obs/cost/meter.py`
Then: `grep -n "COST_UNPRICED_MODEL_BEHAVIOR" -r packages/observability`
Adapt the test below to the real field names before writing it — the point is the behaviour, not these exact identifiers.

- [ ] **Step 2: Write the failing test**

```python
# packages/observability/tests/test_cost_unpriced_local_cli.py
from forge_obs.cost.pricing import price_for


def test_local_cli_model_is_unpriced_not_free():
    """A subscription CLI has real cost and no per-token price.

    Returning 0.0 would make local runs look free and corrupt cost-per-change.
    """
    assert price_for("local-cli") is None


def test_unknown_model_is_also_unpriced():
    assert price_for("some-model-we-have-never-seen") is None
```

- [ ] **Step 3: Run test to verify it fails or already passes**

Run: `uv run pytest packages/observability/tests/test_cost_unpriced_local_cli.py -v`
If it already passes, the existing unpriced path covers this; record that in the commit message and skip to Step 5.

- [ ] **Step 4: Implement**

Ensure `price_for` returns `None` (not `0.0`) for `local-cli` and for unknown models, and that the meter records `unpriced=True` rather than a zero cost.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/observability/tests/test_cost_unpriced_local_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add packages/observability/
git commit -m "feat(cost): mark local-CLI runs unpriced rather than free"
```

---

### Task 10: Operator wiring and documentation

**Files:**
- Modify: `Makefile`
- Modify: `scripts/dev.sh`
- Modify: `.env.example`
- Modify: `deploy/docker/worker.Dockerfile` (no change expected; verify `host.docker.internal` reachability)
- Modify: `README.md`
- Create: `docs/self-hosting/local-cli-provider.md`

**Interfaces:**
- Consumes: Tasks 1–9
- Produces: `make cli-bridge`; `scripts/dev.sh up` detects CLIs and prints bridge instructions

- [ ] **Step 1: Add the Makefile target**

```makefile
cli-bridge: ## Run the host-side local-CLI bridge (dev/self-host only)
	FORGE_CLI_BRIDGE_TOKEN=$${FORGE_CLI_BRIDGE_TOKEN:?set FORGE_CLI_BRIDGE_TOKEN} \
	uv run uvicorn forge_cli_bridge.main:app --host 127.0.0.1 --port $${FORGE_CLI_BRIDGE_PORT:-8099}
```

- [ ] **Step 2: Extend the dev.sh banner**

In the `if [ -z "${FORGE_MODEL_PROVIDER:-}" ]; then` block (`scripts/dev.sh:62`), before the existing text, detect CLIs and offer the bridge:

```bash
  if command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1; then
    cat <<EOF

Local agent CLI detected. To use it instead of an API key:

  export FORGE_CLI_BRIDGE_TOKEN=\$(openssl rand -hex 16)
  make cli-bridge                       # in another terminal, on the host

  Then add to deploy/.env.dev and re-run 'make dev-up':
    FORGE_LOCAL_CLI_ENABLED=1
    FORGE_CLI_BRIDGE_URL=http://host.docker.internal:8099
    FORGE_CLI_BRIDGE_TOKEN=<the same token>
EOF
  fi
```

- [ ] **Step 3: Document the env contract in `.env.example`**

Add beneath the existing BYOK block:

```bash
# ---------------------------------------------------------------------------
# Local agent CLI provider (dev / self-host). UNSET by default. When enabled and
# no BYOK key is configured, Forge drives a locally installed `claude` or `codex`
# CLI as a text oracle, with the CLI's own tool use disabled. Because the worker
# container cannot execute host binaries, this goes through a loopback bridge
# you run on the host (`make cli-bridge`).
# ---------------------------------------------------------------------------
# FORGE_LOCAL_CLI_ENABLED=1
# FORGE_LOCAL_CLI_PREFERENCE=claude,codex
# FORGE_CLI_BRIDGE_URL=http://host.docker.internal:8099
# FORGE_CLI_BRIDGE_TOKEN=
# FORGE_LOCAL_CLI_TIMEOUT_S=600
```

- [ ] **Step 4: Write `docs/self-hosting/local-cli-provider.md`**

Cover: what it is, the arch constraint and why the bridge exists, setup steps, the security posture table from spec §6, and an explicit statement that it is not for shared or production deployments.

- [ ] **Step 5: Verify the worker can reach the host**

Run: `docker exec forge-dev-worker-1 sh -c 'getent hosts host.docker.internal || echo MISSING'`
Expected: an address. If MISSING, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the worker service in the dev compose file and re-run.

- [ ] **Step 6: End-to-end verification**

```bash
export FORGE_CLI_BRIDGE_TOKEN=$(openssl rand -hex 16)
make cli-bridge &
curl -fsS -H "Authorization: Bearer $FORGE_CLI_BRIDGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude","messages":[{"role":"user","content":"Reply with the envelope, content \"pong\", no tool calls."}]}' \
  http://127.0.0.1:8099/v1/complete
```
Expected: JSON with `"content": "pong"` and `"stop_reason": "end_turn"`.

Then add the three variables to `deploy/.env.dev`, run `make dev-up`, and start an agent run from the UI at `http://localhost:8080`. Expected: the run proceeds rather than returning 503.

- [ ] **Step 7: Full gate**

```bash
make lint && make typecheck && uv run pytest -q
```
Expected: all green. Budget ~20 minutes if `packages/db/tests` is included.

- [ ] **Step 8: Commit**

```bash
git add Makefile scripts/dev.sh .env.example README.md docs/self-hosting/local-cli-provider.md
git commit -m "docs(local-cli): operator wiring, env contract, and self-hosting guide"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 architecture constraint / bridge | 7, 8, 10 |
| §3 tool-calling envelope, schema, repair retry, fail loud | 3, 5 |
| §3 tool suppression | 4 (asserted for every backend) |
| §4.1 detect / backends / client / bridge_client | 1, 4, 5, 8 |
| §4.2 bridge app, loopback, token, health | 7 |
| §4.3 config, optional api_key, env contract | 2, 10 |
| §4.4 resolution order | 2 |
| §5 cost unpriced | 9 |
| §6 security posture | 4, 7, 10 |
| §7 error handling table | 5, 8 |
| §8 testing | every task |
| §9 acceptance criteria 1–8 | 2, 4, 5, 7, 9, 10 |

No gaps.

**Placeholder scan:** none. Task 9 Step 1 directs reading the existing meter before writing the test — that is a real instruction with a command, not a deferred decision.

**Type consistency:** `CliName` (Tasks 1, 2, 4, 5, 6, 7); `DetectedCli` (1, 2); `CliResult` (4, 5); `backend_for`/`run_cli` (4, 5); `build_local_cli_prompt`/`parse_local_cli_response`/`local_cli_output_schema` (3, 5); `LocalCliModelClient` (5, 6, 7); `LocalCliBridgeClient` (6, 8); `BridgeSettings` (7). Consistent throughout.

**Known ordering constraint:** Task 6 references `LocalCliBridgeClient` from Task 8. Implement 6 and 8 together, or guard the bridge branch in 6 and unguard it in 8. Flagged in Task 6 Step 3.
