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
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator

from forge_agent.providers import translate
from forge_agent.providers.local_cli.backends import CliResult, backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName
from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ProviderFailureStopReason,
)

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


def _write_prompt_file(directory: str, text: str) -> str:
    """Write ``text`` to ``directory/prompt.txt``, readable by this user only."""
    path = os.path.join(directory, "prompt.txt")
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


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
        model: str | None = None,
        timeout_s: float = 600.0,
        redactor: Callable[[str], str] = _identity,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._cli_name = cli_name
        #: The CLI's own model choice (e.g. ``muse-wonder-full-1.0``), passed as
        #: its ``--model`` flag; ``None`` keeps the CLI's configured default.
        self._cli_model = model
        self._backend = backend_for(cli_name)
        self._timeout_s = timeout_s
        self._redactor = redactor
        self._runner = runner
        #: The namespaced model id every response carries (success *and*
        #: failure). A CLI subscription has no per-token price, so the cost
        #: meter must never see a real API model id for one of these runs: an
        #: objective that pins a per-role model (``AgentObjective.model``) is
        #: what ``AgentRunner`` falls back to when a response reports no model,
        #: which would price a subscription run at that model's API rates
        #: (spec F §5). Matches ``ModelClientConfig``'s own ``local-cli:<cli>``.
        self._model_id = f"local-cli:{cli_name}"

    def complete(self, request: ModelRequest) -> ModelResponse:
        system, user = translate.build_local_cli_prompt(request)
        result = self._invoke(system, user)
        failure = self._failure_for(result)
        if failure is not None:
            return failure

        response = translate.parse_local_cli_response(self._text(result.stdout))
        if response.stop_reason != "parse_error":
            return self._tagged(response)

        _logger.warning("local cli %s returned unparseable output; retrying once", self._cli_name)
        retry = self._invoke(f"{system}\n\n{_REPAIR}", user)
        failure = self._failure_for(retry)
        if failure is not None:
            return failure
        return self._tagged(translate.parse_local_cli_response(self._text(retry.stdout)))

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
        prompt_dir: str | None = None
        try:
            if self._backend.needs_schema_file:
                schema_path = _write_schema_file()
            prompt_path: str | None = None
            if self._backend.needs_prompt_file:
                prompt_dir = tempfile.mkdtemp(prefix="forge-cli-")
                prompt_path = _write_prompt_file(prompt_dir, f"{system}\n\n{user}")
            argv = self._backend.argv(
                system=system,
                user=user,
                schema_path=schema_path,
                model=self._cli_model,
                prompt_path=prompt_path,
            )
            stdin_payload = self._backend.stdin_payload(system=system, user=user)
            return run_cli(
                argv, timeout_s=self._timeout_s, runner=self._runner, input=stdin_payload
            )
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
            if prompt_dir:
                shutil.rmtree(prompt_dir, ignore_errors=True)

    def _tagged(self, response: ModelResponse) -> ModelResponse:
        """Stamp the namespaced ``local-cli:<cli>`` model id onto a response.

        The CLI envelope carries no model id of its own, and an untagged
        response makes ``AgentRunner`` price the run under the objective's
        pinned model instead (see ``_model_id``).
        """
        return response.model_copy(update={"model": self._model_id})

    def _failure(self, reason: ProviderFailureStopReason) -> ModelResponse:
        """A failed turn, carrying the namespaced model id like any other.

        ``reason`` is typed with the shared ``ProviderFailureStopReason``
        literal (``forge_contracts``), which is the same definition the agent
        runtime matches against — a value outside it fails type-checking here
        rather than becoming a silently-successful empty run downstream.
        """
        return ModelResponse(content="", model=self._model_id, stop_reason=reason)

    def _failure_for(self, result: CliResult) -> ModelResponse | None:
        if result.timed_out:
            return self._failure("timeout")
        if result.input_too_large:
            # Distinct from provider_unavailable: the binary is present, the
            # call was simply too large for execve (Fix round 1, minor).
            return self._failure("input_too_large")
        if result.binary_missing:
            # Amendment A: only a synthesized "binary not found" failure means
            # unavailable. A real CLI invocation can legitimately return 124/127
            # as ordinary shell-convention exit codes, so the numeric code alone
            # must never be treated as a sentinel (see backends.CliResult docs).
            return self._failure("provider_unavailable")
        if result.returncode != 0:
            # Amendment C: this branch is reachable only on a non-zero exit;
            # stderr on a *successful* (zero-exit) run is never inspected here,
            # since a healthy CLI can still print startup noise to stderr.
            stderr = result.stderr or ""
            scrubbed = self._scrub(stderr)
            reason: ProviderFailureStopReason = (
                "auth_error" if _looks_like_auth_failure(stderr) else "provider_error"
            )
            _logger.warning("local cli %s failed (%s): %s", self._cli_name, reason, scrubbed)
            return self._failure(reason)
        return None

    def _scrub(self, stderr: str) -> str:
        """Run the injected ``redactor``, degrading to a fixed placeholder on failure.

        The redactor is caller-supplied and could itself misbehave; if it
        raises, the fallback must never be the raw ``stderr`` it was asked to
        scrub, or a redactor bug becomes a secret leak.
        """
        try:
            return self._redactor(stderr)
        except Exception:
            return "[redaction failed]"

    def _text(self, stdout: str) -> str:
        return self._unwrap(self._backend.final_text(stdout))

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
