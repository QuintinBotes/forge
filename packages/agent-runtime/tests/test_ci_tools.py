"""CI tools use the real integration client with a hermetic GitHub transport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from forge_agent.tools import ToolRegistry, agent_tool_belt
from forge_integrations import GitHubClient

CI_TOOLS = {"ci_failures", "ci_job_log", "pr_status"}
SHA = "a" * 40


def _belt(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> ToolRegistry:
    return agent_tool_belt(
        tmp_path,
        run_id="run-1",
        ci_client=GitHubClient(token="test-token", transport=httpx.MockTransport(handler)),
        ci_repo="owner/repo",
    )


def test_ci_tools_absent_without_configured_integration(tmp_path: Path) -> None:
    belt = agent_tool_belt(tmp_path, run_id="run-1")
    assert CI_TOOLS.isdisjoint(belt.names())


@pytest.mark.parametrize("repo", [None, "owner/../repo", "owner/repo?extra=x", "https://host/repo"])
def test_ci_configuration_rejects_partial_or_invalid_repository(
    tmp_path: Path, repo: str | None
) -> None:
    with pytest.raises(ValueError):
        agent_tool_belt(tmp_path, run_id="run-1", ci_client=GitHubClient(), ci_repo=repo)


def test_ci_configuration_rejects_repository_without_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configured together"):
        agent_tool_belt(tmp_path, run_id="run-1", ci_repo="owner/repo")


def test_ci_failures_reads_paginated_file_line_annotations(tmp_path: Path) -> None:
    pages = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/owner/repo/check-runs/42/annotations"
        pages.append(request.url.params["page"])
        if pages[-1] == "1":
            return httpx.Response(200, json=[{"annotation_level": "notice"}] * 100)
        return httpx.Response(
            200,
            json=[
                {
                    "annotation_level": "failure",
                    "path": "src/app.py",
                    "start_line": 12,
                    "message": "assert result == 1",
                }
            ],
        )

    belt = _belt(tmp_path, respond)
    assert all(belt.action_for(name) == "read_repo" for name in CI_TOOLS)
    result = belt.dispatch("ci_failures", {"check_run_id": 42})
    assert result.ok, result.error
    assert result.output == "GitHub owner/repo check run 42:\nsrc/app.py:12: assert result == 1"
    assert result.data["repo"] == "owner/repo"
    assert result.data["check_run_id"] == 42
    assert not result.data["capped"]
    assert pages == ["1", "2"]


def test_ci_failures_caps_annotations(tmp_path: Path) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=[{"annotation_level": "failure", "path": "a", "start_line": 1, "message": "bad"}]
            * 100,
        )

    result = _belt(tmp_path, respond).dispatch("ci_failures", {"check_run_id": 42})
    assert result.ok and result.data["capped"]
    assert len(result.output.splitlines()[1:-1]) == 200
    assert "later failures may be omitted" in result.output.splitlines()[-1]
    assert len(calls) == 2


def test_ci_job_log_follows_redirect_without_forwarding_credentials_and_tails(
    tmp_path: Path,
) -> None:
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        paths.append(request.url.path)
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(302, headers={"Location": "https://logs.example/job.txt"})
        assert request.url.host == "logs.example"
        assert "Authorization" not in request.headers
        return httpx.Response(200, text="\n".join(f"line {i}" for i in range(500)))

    result = _belt(tmp_path, respond).dispatch("ci_job_log", {"job_id": 7})
    assert result.ok, result.error
    assert result.output.splitlines() == [
        "GitHub owner/repo job 7:",
        *(f"line {i}" for i in range(100, 500)),
    ]
    assert result.data == {"repo": "owner/repo", "job_id": 7}
    assert paths == ["/repos/owner/repo/actions/jobs/7/logs", "/job.txt"]


def test_pr_status_includes_state_and_bounded_head_checks(tmp_path: Path) -> None:
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        paths.append(request.url.path)
        if request.url.path.endswith("/pulls/3"):
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "draft": True,
                    "merged": False,
                    "mergeable": None,
                    "head": {"sha": SHA},
                },
            )
        assert request.url.params["per_page"] == "100"
        return httpx.Response(
            200,
            json={
                "total_count": 101,
                "check_runs": [
                    {"id": 42, "name": "unit", "status": "completed", "conclusion": "failure"}
                ]
                * 100,
            },
        )

    result = _belt(tmp_path, respond).dispatch("pr_status", {"pr_number": 3})
    assert result.ok, result.error
    assert result.data["state"] == "open" and result.data["draft"]
    assert not result.data["merged"] and result.data["mergeable"] is None
    assert result.data["repo"] == "owner/repo" and result.data["pr_number"] == 3
    assert len(result.data["check_runs"]) == 100 and result.data["capped"]
    assert result.data["check_runs"][0]["id"] == 42
    assert paths == ["/repos/owner/repo/pulls/3", f"/repos/owner/repo/commits/{SHA}/check-runs"]


@pytest.mark.parametrize(
    ("name", "key"),
    [("ci_failures", "check_run_id"), ("ci_job_log", "job_id"), ("pr_status", "pr_number")],
)
@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "1", "../../other", "https://host"])
def test_ci_tools_reject_invalid_identifiers(
    tmp_path: Path, name: str, key: str, value: object
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request: {request.url}")

    result = _belt(tmp_path, unexpected).dispatch(name, {key: value})
    assert not result.ok
    assert "positive integer" in (result.error or "")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ci_failures", {"check_run_id": 42}),
        ("ci_job_log", {"job_id": 7}),
        ("pr_status", {"pr_number": 3}),
    ],
)
def test_ci_tools_reject_repository_override_and_surface_client_errors(
    tmp_path: Path, name: str, arguments: dict[str, int]
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"message": "integration access denied"})

    belt = _belt(tmp_path, respond)
    assert not belt.dispatch(name, {**arguments, "repo": "foreign/repo"}).ok
    assert not calls
    result = belt.dispatch(name, arguments)
    assert not result.ok and "integration access denied" in (result.error or "")
    assert len(calls) == 1


def test_pr_status_rejects_untrusted_sha_before_followup_request(tmp_path: Path) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"head": {"sha": "../other?query=1"}})

    result = _belt(tmp_path, respond).dispatch("pr_status", {"pr_number": 3})
    assert not result.ok and "invalid PR head SHA" in (result.error or "")
    assert len(calls) == 1
