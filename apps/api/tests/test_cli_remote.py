"""forge-cli remote reads for desktop clients (spec L §2 seam 2, §3, §4)."""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest

from forge_api import cli, cli_remote

TOKEN = "fgk_" + "s" * 32
SERVER = "https://forge.test/api"


def _handler(seen: list[httpx.Request], status: int = 200, body: Any = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else [])

    return httpx.MockTransport(handle)


def _run(
    argv: list[str], env: dict[str, str], transport: httpx.MockTransport
) -> tuple[int, str, str]:
    args = cli.build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()

    def fetcher(a: Any, *, env: dict[str, str]) -> dict[str, Any]:
        return cli_remote.fetch(a, env=env, transport=transport)

    code = cli_remote.run(args, env=env, out=out, err=err, fetcher=fetcher)
    return code, out.getvalue(), err.getvalue()


ENV = {"FORGE_URL": SERVER, "FORGE_TOKEN": TOKEN}


@pytest.mark.parametrize(
    ("argv", "path", "params"),
    [
        (
            ["board", "--project", "p1", "--status", "todo"],
            "/api/board/tasks",
            {"project_id": "p1", "status": "todo", "limit": "100"},
        ),
        (["specs", "--project", "p1"], "/api/projects/p1/specs", {}),
        (["approvals"], "/api/approvals", {"status": "pending"}),
        (["runs", "r1"], "/api/agent/runs/r1", {}),
    ],
)
def test_each_command_reads_its_endpoint_with_the_bearer_token(
    argv: list[str], path: str, params: dict[str, str]
) -> None:
    seen: list[httpx.Request] = []
    code, _out, err = _run([*argv, "--json"], ENV, _handler(seen, body={"ok": True}))
    assert code == 0, err
    (req,) = seen
    assert req.url.path == path
    assert dict(req.url.params) == params
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


def test_json_envelope_carries_schema_and_provenance() -> None:
    tasks = [{"key": "T-1", "status": "todo", "title": "Write spec"}]
    code, out, _ = _run(["board", "--json"], ENV, _handler([], body=tasks))
    assert code == 0
    envelope = json.loads(out)
    assert envelope["schema"] == "forge.cli/v1"
    assert envelope["kind"] == "board"
    assert envelope["observed_by"] == SERVER
    assert envelope["enforceable"] is False
    assert envelope["recorded_at"].endswith("+00:00")
    assert envelope["data"] == tasks


def test_table_output_names_the_source() -> None:
    tasks = [{"key": "T-1", "status": "todo", "title": "Write spec"}]
    code, out, _ = _run(["board"], ENV, _handler([], body=tasks))
    assert code == 0
    assert "T-1\ttodo\tWrite spec" in out
    assert f"(1 from {SERVER} at " in out


def test_token_is_read_from_the_named_variable() -> None:
    seen: list[httpx.Request] = []
    env = {"FORGE_URL": SERVER, "TERVIN_FORGE_TOKEN": TOKEN}
    code, _, err = _run(["approvals", "--token-env", "TERVIN_FORGE_TOKEN"], env, _handler(seen))
    assert code == 0, err
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FORGE_TOKEN": TOKEN}, "set FORGE_URL"),
        ({"FORGE_URL": SERVER}, "set the FORGE_TOKEN environment variable"),
    ],
)
def test_missing_configuration_exits_2_without_calling(env: dict[str, str], message: str) -> None:
    seen: list[httpx.Request] = []
    code, out, err = _run(["approvals"], env, _handler(seen))
    assert code == cli_remote.EXIT_CONFIG
    assert message in err
    assert seen == [] and out == ""


def test_server_errors_exit_3_with_detail_and_never_print_the_token() -> None:
    code, out, err = _run(
        ["runs", "nope", "--json"], ENV, _handler([], 404, {"detail": "no run nope"})
    )
    assert code == cli_remote.EXIT_HTTP
    assert "404 no run nope" in err
    assert TOKEN not in out + err


def test_unreachable_server_exits_3() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    code, _, err = _run(["approvals"], ENV, httpx.MockTransport(refuse))
    assert code == cli_remote.EXIT_HTTP
    assert "cannot reach" in err and TOKEN not in err


def test_main_dispatches_remote_groups(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    transport = _handler(seen, body=[])
    real_fetch = cli_remote.fetch
    monkeypatch.setattr(
        cli_remote, "fetch", lambda a, *, env: real_fetch(a, env=env, transport=transport)
    )
    monkeypatch.setenv("FORGE_URL", SERVER)
    monkeypatch.setenv("FORGE_TOKEN", TOKEN)
    assert cli.main(["approvals", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "approvals"
    assert seen[0].url.path == "/api/approvals"
