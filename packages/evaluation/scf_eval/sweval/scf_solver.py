"""Drive a running SCF instance as a forge-factory-bench solver (FR-015, #197).

The adapter targets a development instance's direct run path. It submits a case's
issue text to ``POST /agent/runs`` against a repository connected to the instance,
waits for the run to finish, and collects the change from the branch the run pushed
(``artifacts["branch"]``), read from a local clone of that repository. Each case's
repository tree must already be on its base branch, ``bench/<case id>`` by default.

A run that does not succeed, or that made no commits, yields an empty patch, so the
case is scored not resolved. An instance that cannot be reached, or that refuses the
run (409), raises, so the harness records an infrastructure error with the reason.
Scoring a governed instance is not supported yet: outside development the API
refuses a run without a task, and a governed run delivers its branch only after
its workflow's gates and approvals.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from scf_eval.golden import GoldenCase

#: Run statuses after which a run changes no more.
_TERMINAL = frozenset({"succeeded", "failed", "escalated", "cancelled"})


class ScfInstanceSolver:
    """A :class:`~scf_eval.sweval.bench.Solver` backed by a running SCF instance."""

    name = "scf-instance"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        repository: str,
        clone_dir: str | Path,
        base_branch: str = "bench/{case_id}",
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
        poll_interval_s: float = 10.0,
        timeout_s: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.models: tuple[str, ...] = (model,) if model else ()
        self.last_cost_usd: float | None = None
        #: Results from another instance or repository are never reused on resume.
        self.key = f"{base_url.rstrip('/')} {repository}"
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._repository = repository
        self._clone = Path(clone_dir)
        self._base_branch = base_branch
        self._model = model
        self._transport = transport
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._sleep = sleep

    def __call__(self, case: GoldenCase) -> dict[str, str]:
        base = self._base_branch.format(case_id=case.id)
        run = self._run_to_end(case, base)
        artifacts = run.get("artifacts") or {}
        self.last_cost_usd = (artifacts.get("model_usage") or {}).get("cost_usd")
        branch = artifacts.get("branch")
        # A run that made no commits never pushes its branch: nothing to collect.
        no_changes = (artifacts.get("pull_request") or {}).get("status") == "no_changes"
        if run.get("status") != "succeeded" or not branch or no_changes:
            return {}
        self._git("fetch", "--quiet", "origin", base, branch)
        # shortcut: a deleted file cannot be expressed as a file write, so it is dropped;
        # extend the solve contract if a case ever needs a deletion.
        changed = self._git(
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=AM",
            f"origin/{base}...origin/{branch}",
        )
        return {path: self._git("show", f"origin/{branch}:{path}") for path in changed.splitlines()}

    def _run_to_end(self, case: GoldenCase, base: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "objective": case.query,
            "repo_targets": [{"repo": self._repository, "base_branch": base, "role": "primary"}],
        }
        if self._model:
            body["model"] = self._model
        headers = {"Authorization": f"Bearer {self._token}"}
        with httpx.Client(
            base_url=self._base_url, transport=self._transport, headers=headers, timeout=30.0
        ) as client:
            created = client.post("/agent/runs", json=body)
            if created.status_code == httpx.codes.CONFLICT:
                raise RuntimeError(
                    f"the instance refused the run: {_detail(created)}; this adapter drives a "
                    "development instance's direct path, a governed instance is not supported"
                )
            created.raise_for_status()
            run_id = created.json()["run_id"]
            deadline = time.monotonic() + self._timeout_s
            while True:
                resp = client.get(f"/agent/runs/{run_id}")
                # A governed run's agent run reads as not found until the workflow's gates pass.
                if resp.status_code != httpx.codes.NOT_FOUND:
                    resp.raise_for_status()
                    run: dict[str, Any] = resp.json()
                    if run.get("status") in _TERMINAL:
                        return run
                if time.monotonic() > deadline:
                    raise TimeoutError(f"run {run_id} did not finish within {self._timeout_s:.0f}s")
                self._sleep(self._poll_interval_s)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self._clone, check=True, capture_output=True, text=True
        ).stdout


def _detail(resp: httpx.Response) -> str:
    """The API's reason for an error response (its ``detail``), else the raw body."""
    try:
        return str(resp.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return resp.text


__all__ = ["ScfInstanceSolver"]
