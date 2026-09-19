import datetime
import errno
import json
import logging
import os
import subprocess
import tempfile

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


def test_input_too_large_is_distinguished_from_provider_unavailable():
    """Fix round 1, minor: E2BIG means the binary is present but the call was
    too large for execve — must not be reported as provider_unavailable
    (which would wrongly tell the caller the CLI is missing).
    """

    def run(argv, **kwargs):
        raise OSError(errno.E2BIG, "Argument list too long")

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run)
    assert client.complete(_request()).stop_reason == "input_too_large"


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


def test_stderr_passes_through_redactor(caplog):
    """``response.content`` is unconditionally "" on every failure path, so
    asserting the secret is absent from it is vacuous (it cannot fail). What
    actually matters: the redactor sees the raw stderr, and only the
    redacted value it returns ever reaches the log — never the raw secret.
    """
    seen: list[str] = []

    def redactor(value: str) -> str:
        seen.append(value)
        return "[scrubbed]"

    client = LocalCliModelClient(
        cli_name=CliName.claude,
        runner=_runner("", returncode=2, stderr="key sk-abc123"),
        redactor=redactor,
    )
    with caplog.at_level(logging.WARNING, logger="forge_agent.providers.local_cli"):
        client.complete(_request())

    assert seen == ["key sk-abc123"], "redactor must be consulted with the raw stderr"
    assert "sk-abc123" not in caplog.text, "the raw secret must never reach the log"
    assert "[scrubbed]" in caplog.text, "only the redacted value must reach the log"


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


# -- Fix round 1: schema-file write must never escape complete() ------------ #
#
# Only the codex backend needs a schema file (``needs_schema_file`` is True);
# the schema write happens before ``run_cli`` is ever reached, so none of
# ``run_cli``'s own "never raises" handling covers it.


def test_schema_write_mkstemp_failure_is_provider_error(monkeypatch):
    """A disk-full/unwritable-temp-dir failure inside ``tempfile.mkstemp``
    must fold into a diagnostic ``ModelResponse``, not escape ``complete()``.
    """

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "mkstemp", boom)
    client = LocalCliModelClient(cli_name=CliName.codex, runner=_runner(ENVELOPE))
    response = client.complete(_request())
    assert response.stop_reason == "provider_error"


def test_schema_write_mid_write_failure_is_provider_error_and_leaves_no_temp_file(
    monkeypatch,
):
    """A failure while writing the schema (e.g. ``json.dump`` mid-write) must
    also fold into ``provider_error`` rather than escape ``complete()`` — and
    the half-written temp file it created must not be left behind.
    """
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    def boom_dump(*args, **kwargs):
        raise OSError("io error mid-write")

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(json, "dump", boom_dump)

    client = LocalCliModelClient(cli_name=CliName.codex, runner=_runner(ENVELOPE))
    response = client.complete(_request())

    assert response.stop_reason == "provider_error"
    assert created, "mkstemp should have been called for the codex schema file"
    assert not os.path.exists(created[0]), "the half-written temp file must not be left behind"


def test_schema_file_removed_after_run_cli_failure(monkeypatch):
    """The schema temp file must be cleaned up even when the CLI invocation
    itself fails (not just on the happy path)."""
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)

    client = LocalCliModelClient(
        cli_name=CliName.codex, runner=_runner("", returncode=2, stderr="boom")
    )
    response = client.complete(_request())

    assert response.stop_reason == "provider_error"
    assert created, "mkstemp should have been called for the codex schema file"
    assert not os.path.exists(created[0]), (
        "the schema temp file must be removed after a CLI failure"
    )


# -- Fix round 2: whole-contract re-trace found holes in translate.py's ----- #
# CLI-controlled parsing and in an unguarded injected redactor.


def test_malformed_usage_in_envelope_does_not_crash_complete():
    """CRITICAL: parse_local_cli_response did ``int(usage["input_tokens"])``
    unguarded on a field that comes straight from the CLI's own output. A
    non-integer value must fold into parse_error like any other schema
    violation, not escape complete() as a raw ValueError.
    """
    raw = '{"content":"hi","tool_calls":[],"stop_reason":"end_turn","usage":{"input_tokens":"abc"}}'
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(raw))
    response = client.complete(_request())
    assert response.stop_reason == "parse_error"


def test_non_serialisable_tool_schema_does_not_crash_prompt_building():
    """IMPORTANT: build_local_cli_prompt did ``json.dumps(request.tools)``
    unguarded. A tool schema containing a non-JSON-serialisable value (e.g. a
    stray datetime) must degrade rather than crash before the CLI is ever
    invoked.
    """
    request = ModelRequest(
        model="claude",
        messages=[ModelMessage(role="user", content="hi")],
        tools=[{"name": "t", "when": datetime.datetime.now()}],
    )
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(ENVELOPE))
    response = client.complete(request)
    assert response.content == "ok"


def test_raising_redactor_degrades_to_placeholder_without_leaking_stderr(caplog):
    """MINOR: the injected redactor is caller-supplied; if it misbehaves,
    complete() must still never raise, and the fallback must never be the
    raw stderr it was supposed to scrub (that would turn a redactor bug into
    a secret leak).
    """

    def bad_redactor(value: str) -> str:
        raise RuntimeError("redactor exploded")

    client = LocalCliModelClient(
        cli_name=CliName.claude,
        runner=_runner("", returncode=2, stderr="key sk-abc123"),
        redactor=bad_redactor,
    )
    with caplog.at_level(logging.WARNING, logger="forge_agent.providers.local_cli"):
        response = client.complete(_request())

    assert response.stop_reason == "provider_error"
    assert "sk-abc123" not in caplog.text, "a raising redactor must never leak the raw stderr"


# -- namespaced model id (final review, I2) --------------------------------- #
#
# Neither local client used to set ``ModelResponse.model``, so ``AgentRunner``
# fell back to ``AgentObjective.model`` and priced a subscription run at that
# (real) model's API rates -- spec F §5's exact failure mode. The id must be
# present on EVERY return path, success and failure alike. The end-to-end
# assertion that it reaches the cost meter lives in
# ``tests/test_local_cli_cost_attribution.py``.


def test_model_id_is_namespaced_on_success():
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(ENVELOPE))
    assert client.complete(_request()).model == "local-cli:claude"


def test_model_id_names_the_cli_that_ran():
    client = LocalCliModelClient(cli_name=CliName.codex, runner=_runner(ENVELOPE))
    assert client.complete(_request()).model == "local-cli:codex"


def test_model_id_survives_a_repair_retry():
    outputs = iter(["garbage", ENVELOPE])

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=next(outputs), stderr="")

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run)
    assert client.complete(_request()).model == "local-cli:claude"


def _failing_runners():
    """One runner per failure path in §7, keyed by the stop_reason it produces."""

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    def missing(argv, **kwargs):
        raise FileNotFoundError("nope")

    def too_large(argv, **kwargs):
        raise OSError(errno.E2BIG, "Argument list too long")

    return {
        "timeout": timed_out,
        "provider_unavailable": missing,
        "input_too_large": too_large,
        "provider_error": _runner("", returncode=2, stderr="boom"),
        "auth_error": _runner("", returncode=1, stderr="Please run `claude login`"),
        "parse_error": _runner("not json"),
    }


def test_model_id_is_namespaced_on_every_failure_path():
    for expected_reason, runner in _failing_runners().items():
        client = LocalCliModelClient(cli_name=CliName.claude, runner=runner, timeout_s=1.0)
        response = client.complete(_request())
        assert response.stop_reason == expected_reason
        assert response.model == "local-cli:claude", (
            f"{expected_reason} must still be metered as a local-CLI run"
        )
