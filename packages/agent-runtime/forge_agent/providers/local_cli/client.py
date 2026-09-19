"""A ``ModelClient`` that drives a locally installed agent CLI as a text oracle.

Never raises out of ``complete``: every failure is a ``ModelResponse`` carrying a
diagnostic ``stop_reason``, because the agent loop distinguishes "did nothing" from
"could not be read" only by that field. An unparseable reply gets exactly one
repair retry (re-asking the CLI for the envelope only) before giving up with
``parse_error`` — never an empty ``tool_calls`` list, which would be
indistinguishable from the model deliberately choosing to do nothing.
"""

from __future__ import annotations

import contextlib
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

#: Specific auth-failure phrases the supported CLIs are known to emit. Kept
#: as exact phrases (not bare words like "login" or "authentication") because
#: a live `codex exec` run prints unrelated MCP "auth required" startup
#: warnings (for e.g. unconfigured gitlab/github-copilot connectors) on
#: stderr even on a healthy invocation; a loose scan would mislabel that
#: noise as this CLI's own auth failure and send an operator to
#: re-authenticate something that was never broken (see Amendment B, task 5).
_AUTH_MARKERS = (
    "please run `claude login`",
    "please run `codex login`",
    "run `claude login` to authenticate",
    "run `codex login` to authenticate",
    "not logged in",
    "authentication_error",
    "invalid api key",
)


def _identity(value: str) -> str:
    return value


def _looks_like_auth_failure(stderr: str) -> bool:
    """Classify ``stderr`` (already known non-empty on a non-zero exit) as auth-shaped.

    Gated on specific phrasing rather than any mention of "login" or
    "authentication" in isolation, plus a narrow proximity check ("please
    run" co-occurring with "login") to catch phrasing variants without
    matching an unrelated sentence that merely mentions login/authentication.
    """
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return True
    return "please run" in lowered and "login" in lowered


def _write_schema_file() -> str:
    """Write the envelope JSON Schema to a fresh temp file; return its path.

    On any failure, cleans up after itself before re-raising (to the caller,
    ``LocalCliModelClient._invoke``, which folds the exception into a
    diagnostic ``CliResult``):

    * ``os.fdopen`` only hands ownership of the raw ``mkstemp`` fd to the
      file object it returns once it *succeeds* — if ``os.fdopen`` itself
      raises, that fd is never wrapped and would otherwise leak. It is
      closed explicitly on the failure path.
    * a write failure (e.g. ``json.dump`` mid-write) leaves a half-written
      file on disk; it is unlinked rather than left behind. (``with
      os.fdopen(...)`` already closes the fd itself on this path via
      its own ``__exit__``; closing an already-closed fd is suppressed.)
    """
    handle, path = tempfile.mkstemp(suffix=".json", prefix="forge-cli-")
    try:
        with os.fdopen(handle, "w") as fh:
            json.dump(translate.local_cli_output_schema(), fh)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(handle)
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path


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
        """Build argv (writing a schema temp file first if the backend needs
        one) and run it. Like ``run_cli``, this never raises: a schema-file
        write failure (disk full, an unwritable temp dir, an I/O error
        mid-write) is folded into a generic-failure ``CliResult`` — the same
        convention ``run_cli`` itself uses for an unexpected exception — so
        ``complete()``'s never-raise contract holds even for the part of the
        invocation that happens before ``run_cli`` is ever reached.
        """
        schema_path: str | None = None
        try:
            if self._backend.needs_schema_file:
                schema_path = _write_schema_file()
            argv = self._backend.argv(system=system, user=user, schema_path=schema_path)
            return run_cli(argv, timeout_s=self._timeout_s, runner=self._runner)
        except Exception as exc:
            # Mirrors run_cli's own "never raises" contract: anything that can go
            # wrong building/writing the schema file or constructing argv must
            # fold into a CliResult, not escape complete(). 1 is the same
            # generic-failure marker run_cli uses for its own catch-all.
            return CliResult(stdout="", stderr=str(exc), returncode=1, timed_out=False)
        finally:
            if schema_path:
                with contextlib.suppress(OSError):
                    os.unlink(schema_path)

    def _failure_for(self, result: CliResult) -> ModelResponse | None:
        if result.timed_out:
            return ModelResponse(content="", stop_reason="timeout")
        if result.binary_missing:
            # Amendment A: only a synthesized "binary not found" failure means
            # unavailable. A real CLI invocation can legitimately return 124/127
            # as ordinary shell-convention exit codes, so the numeric code alone
            # must never be treated as a sentinel (see backends.CliResult docs).
            return ModelResponse(content="", stop_reason="provider_unavailable")
        if result.returncode != 0:
            # Amendment C: this branch is reachable only on a non-zero exit;
            # stderr on a *successful* (zero-exit) run is never inspected here,
            # since a healthy CLI can still print startup noise to stderr.
            stderr = result.stderr or ""
            scrubbed = self._redactor(stderr)
            reason = "auth_error" if _looks_like_auth_failure(stderr) else "provider_error"
            _logger.warning("local cli %s failed (%s): %s", self._cli_name, reason, scrubbed)
            return ModelResponse(content="", stop_reason=reason)
        return None

    @staticmethod
    def _unwrap(stdout: str) -> str:
        """Unwrap a CLI result wrapper (e.g. claude's ``{"type":"result",...}``)
        so the inner envelope can be parsed."""
        try:
            outer = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return stdout
        if isinstance(outer, dict) and "result" in outer:
            inner = outer["result"]
            return inner if isinstance(inner, str) else json.dumps(inner)
        return stdout
