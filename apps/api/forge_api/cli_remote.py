"""Remote read commands for desktop clients (spec L §2 seam 2, ADR-0015).

``forge-cli board|specs|approvals|runs`` read a forge team server over HTTP and print
either a table or, with ``--json``, one versioned envelope a client such as tervin
draws natively. The server comes from ``FORGE_URL`` (or ``--url``); the token comes
from the environment variable named by ``--token-env`` (default ``FORGE_TOKEN``), so
a client that stores no secrets can point at its own variable. The token is never
printed, logged, or put on the command line.

Every envelope carries provenance (spec L §4): ``observed_by`` (the server that
answered), ``recorded_at`` (when it answered), and ``enforceable: false`` (a read is
an observation, never a gate).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
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
    """Register the four remote read commands on the forge-cli parser."""

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", dest="as_json")
        p.add_argument("--url", default=None, help="server base URL (default: $FORGE_URL)")
        p.add_argument(
            "--token-env",
            default=DEFAULT_TOKEN_ENV,
            help="name of the environment variable holding the API token",
        )
        p.add_argument("--timeout", type=float, default=15.0)

    board = sub.add_parser("board", help="list board tasks from a forge server")
    board.add_argument("--project", default=None)
    board.add_argument("--status", action="append", default=None)
    board.add_argument("--limit", type=int, default=100)
    common(board)
    specs = sub.add_parser("specs", help="a project's spec overview from a forge server")
    specs.add_argument("--project", required=True)
    common(specs)
    approvals = sub.add_parser("approvals", help="the approval inbox from a forge server")
    approvals.add_argument("--status", default="pending")
    common(approvals)
    runs = sub.add_parser("runs", help="one agent run from a forge server")
    runs.add_argument("run_id")
    common(runs)


REMOTE_GROUPS = frozenset({"board", "specs", "approvals", "runs"})


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
        (err or sys.stderr).write(f"forge-cli {args.group}: {exc}\n")
        return exc.code
    render(envelope, as_json=args.as_json, out=out or sys.stdout)
    return EXIT_OK


__all__ = ["REMOTE_GROUPS", "SCHEMA", "RemoteError", "add_parsers", "fetch", "render", "run"]
