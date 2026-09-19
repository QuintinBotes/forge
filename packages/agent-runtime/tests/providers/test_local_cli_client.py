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
    wrapper = f'{{"type":"result","result":{ENVELOPE}}}'
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


# -- Controller amendment regression tests ---------------------------------- #


def test_returncode_127_without_binary_missing_is_provider_error():
    """Amendment A: 124/127 are shell conventions a real CLI can legitimately
    return; only a synthesized ``CliResult.binary_missing`` means the binary
    itself could not be found. A runner that returns a genuine
    ``CompletedProcess(returncode=127)`` (as opposed to raising
    ``FileNotFoundError``) must not be misclassified as unavailable.
    """
    client = LocalCliModelClient(
        cli_name=CliName.claude, runner=_runner("", returncode=127, stderr="boom")
    )
    assert client.complete(_request()).stop_reason == "provider_error"


def test_mcp_auth_warning_noise_is_not_an_auth_error():
    """codex prints MCP auth-required warnings on startup even when healthy.

    Misreading that as auth_error sends an operator to re-authenticate
    something that was never broken.
    """
    noise = (
        "WARN mcp server 'gitlab': auth required, skipping\n"
        "WARN mcp server 'github-copilot': auth required, skipping\n"
        "error: something unrelated failed\n"
    )
    client = LocalCliModelClient(
        cli_name=CliName.codex, runner=_runner("", returncode=1, stderr=noise)
    )
    assert client.complete(_request()).stop_reason == "provider_error"


def test_genuine_auth_failure_is_still_detected():
    client = LocalCliModelClient(
        cli_name=CliName.claude,
        runner=_runner("", returncode=1, stderr="Please run `claude login` to authenticate"),
    )
    assert client.complete(_request()).stop_reason == "auth_error"


def test_zero_exit_with_noisy_stderr_parses_normally():
    """Amendment C: a successful run can still print noise to stderr; a zero
    exit must never be treated as a failure regardless of stderr content.
    """
    noise = "WARN mcp server 'gitlab': auth required, skipping\n"
    client = LocalCliModelClient(
        cli_name=CliName.claude, runner=_runner(ENVELOPE, returncode=0, stderr=noise)
    )
    response = client.complete(_request())
    assert response.content == "ok"
    assert response.stop_reason == "end_turn"
