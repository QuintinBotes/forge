"""Remote read commands for desktop clients (spec L §2 seam 2, ADR-0015).

``scf-cli board|specs|approvals|runs`` read an SCF team server over HTTP and print
either a table or, with ``--json``, one versioned envelope a client such as tervin
draws natively. The server comes from ``FORGE_URL`` (or ``--url``); the token comes
from the environment variable named by ``--token-env`` (default ``FORGE_TOKEN``), so
a client that stores no secrets can point at its own variable. The token is never
printed, logged, or put on the command line.

``scf-cli login`` obtains such a token with a device sign-in (#207): it shows the
person a short code to approve, and prints only ``export NAME=<token>`` to standard
output, for ``eval "$(scf-cli login --token-env NAME)"``. It writes the token nowhere.

Every envelope carries provenance (spec L §4): ``observed_by`` (the server that
answered), ``recorded_at`` (when it answered), and ``enforceable: false`` (a read is
an observation, never a gate).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

import httpx

#: Bumped only on a breaking change to the envelope.
SCHEMA = "forge.cli/v1"
DEFAULT_TOKEN_ENV = "FORGE_TOKEN"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_HTTP = 3


class RemoteError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _endpoint(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """The server path and query for one command."""
    if args.group == "board":
        params: dict[str, Any] = {"limit": args.limit}
        if args.project:
            params["project_id"] = args.project
        if args.status:
            params["status"] = args.status
        return "/board/tasks", params
    if args.group == "specs":
        return f"/projects/{args.project}/specs", {}
    if args.group == "approvals":
        return "/approvals", ({"status": args.status} if args.status else {})
    return f"/agent/runs/{args.run_id}", {}


def fetch(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Call the server and wrap its answer in the provenance envelope."""
    base = (args.url or env.get("FORGE_URL") or "").rstrip("/")
    if not base:
        raise RemoteError("no server: set FORGE_URL or pass --url", EXIT_CONFIG)
    token = env.get(args.token_env, "")
    if not token:
        raise RemoteError(f"no token: set the {args.token_env} environment variable", EXIT_CONFIG)
    path, params = _endpoint(args)
    with httpx.Client(base_url=base, transport=transport, timeout=args.timeout) as client:
        try:
            resp = client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise RemoteError(f"cannot reach {base}: {type(exc).__name__}", EXIT_HTTP) from exc
    if resp.status_code >= 400:
        detail = ""
        with contextlib.suppress(ValueError):
            detail = str(resp.json().get("detail", ""))
        raise RemoteError(f"{path} returned {resp.status_code} {detail}".strip(), EXIT_HTTP)
    return {
        "schema": SCHEMA,
        "kind": args.group,
        "observed_by": base,
        "recorded_at": datetime.now(UTC).isoformat(),
        "enforceable": False,
        "data": resp.json(),
    }


def _row(kind: str, item: Mapping[str, Any]) -> str:
    if kind == "board":
        return f"{item.get('key', '')}\t{item.get('status', '')}\t{item.get('title', '')}"
    if kind == "approvals":
        return f"{item.get('id', '')}\t{item.get('gate_type', '')}\t{item.get('status', '')}"
    return json.dumps(item, default=str)


def render(envelope: Mapping[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        json.dump(envelope, out, indent=2, sort_keys=True, default=str)
        out.write("\n")
        return
    data = envelope["data"]
    items = data if isinstance(data, list) else [data]
    for item in items:
        out.write(_row(envelope["kind"], item) + "\n")
    out.write(f"({len(items)} from {envelope['observed_by']} at {envelope['recorded_at']})\n")


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the four remote read commands and ``login`` on the scf-cli parser."""

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", dest="as_json")
        p.add_argument("--url", default=None, help="server base URL (default: $FORGE_URL)")
        p.add_argument(
            "--token-env",
            default=DEFAULT_TOKEN_ENV,
            help="name of the environment variable holding the API token",
        )
        p.add_argument("--timeout", type=float, default=15.0)

    board = sub.add_parser("board", help="list board tasks from an SCF server")
    board.add_argument("--project", default=None)
    board.add_argument("--status", action="append", default=None)
    board.add_argument("--limit", type=int, default=100)
    common(board)
    specs = sub.add_parser("specs", help="a project's spec overview from an SCF server")
    specs.add_argument("--project", required=True)
    common(specs)
    approvals = sub.add_parser("approvals", help="the approval inbox from an SCF server")
    approvals.add_argument("--status", default="pending")
    common(approvals)
    runs = sub.add_parser("runs", help="one agent run from an SCF server")
    runs.add_argument("run_id")
    common(runs)
    login = sub.add_parser(
        LOGIN, help="get a token that starts runs by approving a code; print a shell line"
    )
    login.add_argument("--url", default=None, help="server base URL (default: $FORGE_URL)")
    login.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="name of the environment variable the printed line sets",
    )
    login.add_argument("--client-name", default="scf-cli", help="the name the approver sees")
    login.add_argument("--timeout", type=float, default=15.0)


REMOTE_GROUPS = frozenset({"board", "specs", "approvals", "runs"})
LOGIN = "login"
#: ``eval`` runs the printed line, so the variable name must be a plain shell name.
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> httpx.Response:
    try:
        return client.post(path, json=body)
    except httpx.HTTPError as exc:
        raise RemoteError(
            f"cannot reach {client.base_url}: {type(exc).__name__}", EXIT_HTTP
        ) from exc


def _device_sign_in(
    client: httpx.Client, client_name: str, err: TextIO, sleep: Callable[[float], None]
) -> str:
    """RFC 8628: show the person the user code, then poll until they approve or deny it."""
    resp = _post(client, "/auth/device/code", {"client_name": client_name})
    if resp.status_code != 200:
        raise RemoteError(f"/auth/device/code returned {resp.status_code}", EXIT_HTTP)
    started = resp.json()
    user_code = started["user_code"]
    err.write(
        f"To let {client_name!r} start runs as you, approve code {user_code} within "
        f"{started['expires_in'] // 60} minutes. While signed in to SCF, send\n"
        f'  POST {started["verification_uri"]}  {{"user_code": "{user_code}"}}\n'
        "Approve only a code this command printed for you.\n"
    )
    interval = float(started["interval"])
    while True:
        sleep(interval)
        resp = _post(client, "/auth/device/token", {"device_code": started["device_code"]})
        if resp.status_code == 200:
            err.write("Approved.\n")
            return str(resp.json()["access_token"])
        answer: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            answer = resp.json()
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5  # RFC 8628 §3.5
            continue
        detail = answer.get("error_description", "")
        raise RemoteError(f"no token: {error or resp.status_code} {detail}".strip(), EXIT_HTTP)


def login(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run a device sign-in and print one line, ``export NAME=<token>``, to standard output.

    Everything for the person goes to standard error, so the output can be ``eval``-ed.
    """
    env = os.environ if env is None else env
    err = err or sys.stderr
    try:
        if not _SHELL_NAME.fullmatch(args.token_env):
            raise RemoteError(
                f"--token-env must be a shell variable name, not {args.token_env!r}", EXIT_CONFIG
            )
        base = (args.url or env.get("FORGE_URL") or "").rstrip("/")
        if not base:
            raise RemoteError("no server: set FORGE_URL or pass --url", EXIT_CONFIG)
        with httpx.Client(base_url=base, transport=transport, timeout=args.timeout) as client:
            token = _device_sign_in(client, args.client_name, err, sleep)
    except RemoteError as exc:
        err.write(f"scf-cli login: {exc}\n")
        return exc.code
    (out or sys.stdout).write(f"export {args.token_env}={shlex.quote(token)}\n")
    return EXIT_OK


def run(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    fetcher: Callable[..., dict[str, Any]] | None = None,
) -> int:
    try:
        envelope = (fetcher or fetch)(args, env=os.environ if env is None else env)
    except RemoteError as exc:
        (err or sys.stderr).write(f"scf-cli {args.group}: {exc}\n")
        return exc.code
    render(envelope, as_json=args.as_json, out=out or sys.stdout)
    return EXIT_OK


__all__ = [
    "LOGIN",
    "REMOTE_GROUPS",
    "SCHEMA",
    "RemoteError",
    "add_parsers",
    "fetch",
    "login",
    "render",
    "run",
]
