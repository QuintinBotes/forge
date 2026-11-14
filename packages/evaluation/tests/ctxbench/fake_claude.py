#!/usr/bin/env python3
"""A fake ``claude`` for the ctxbench tests: never calls a model or the network.

It reads the prompt from stdin and the MCP config named by ``--mcp-config``,
then prints stream-json like ``claude --print --output-format stream-json
--verbose``. Behaviour is chosen by the prompt and the config only (the
harness passes no other environment):

* ``--version`` prints a version.
* A judge prompt (``--model claude-opus-*``): ``dont_know`` when the answer
  says it does not know, ``wrong`` when it contains ``FAKE-WRONG``, and
  ``correct`` otherwise; a question containing ``BADJUDGE`` gets prose, not JSON.
  It exits 3 with no result when its ``<answer>`` block holds more than the
  answer (sources, tool names or the row's JSON).
* An agent prompt: with no MCP server it answers "I don't know"; with the
  forge server it calls ``mcp__forge__assemble_context`` once, reports the
  envelope (rerank ``fallback`` when the question contains ``FALLBACK``) and
  answers with a JSON object. A server URL containing ``unreachable`` reports
  the server as failed. A question containing ``SLOW`` sleeps for 30 s.
  ``TOOLERROR`` makes the forge tool's result an error, ``STRAYTOOL`` lists a
  built-in tool in the init event, ``MALFORMED`` emits an event of the wrong
  shape, and ``STOP:<subtype>`` ends an agent run with that failed result
  subtype (a budget or turn-limit stop).

The init event also carries ``fake`` facts the tests assert on: the argv,
the environment's keys, the working directory and its ``origin``, the HOME
settings file, the MCP config file's mode and a hash of its
Authorization header (never the token itself).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("9.9.9 (Claude Code, fake)")
        return 0
    prompt = sys.stdin.read()
    model = flag(argv, "--model") or ""
    config_path = flag(argv, "--mcp-config") or ""
    with open(config_path, encoding="utf-8") as handle:
        servers = json.load(handle)["mcpServers"]
    statuses = [
        {"name": name, "status": "failed" if "unreachable" in spec.get("url", "") else "connected"}
        for name, spec in servers.items()
    ]
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    ).stdout.strip()
    settings = os.path.join(os.environ.get("HOME", ""), ".claude", "settings.json")
    emit(
        {
            "type": "system",
            "subtype": "init",
            "model": model,
            "tools": [f"mcp__{s['name']}__assemble_context" for s in statuses]
            + ["Bash"] * ("STRAYTOOL" in prompt),
            "mcp_servers": statuses,
            "fake": {
                "argv": argv,
                "env_keys": sorted(os.environ),
                "cwd": os.getcwd(),
                "origin": origin,
                "home_settings": Path(settings).read_text(encoding="utf-8"),
                "mcp_config_mode": oct(stat.S_IMODE(os.stat(config_path).st_mode)),
                "authorization_sha256": [
                    hashlib.sha256(
                        spec.get("headers", {}).get("Authorization", "").encode()
                    ).hexdigest()
                    for spec in servers.values()
                ],
            },
        }
    )
    if "SLOW" in prompt:
        time.sleep(30)
    if "MALFORMED" in prompt:
        emit({"type": "assistant", "message": "not an object"})
    calls = 0
    tool_failed = "TOOLERROR" in prompt
    if model.startswith("claude-opus"):
        answer = re.search(r"<answer>\n(.*)\n</answer>", prompt, re.DOTALL)
        text = answer.group(1) if answer else ""
        if any(extra in text for extra in ('"sources"', "fake:1", "mcp__", '"tool_calls"')):
            print("the <answer> block holds more than the answer", file=sys.stderr)
            return 3
        if "BADJUDGE" in prompt:
            result = "I think this answer is fine."
        else:
            verdict = (
                "dont_know"
                if "don't know" in text
                else "wrong"
                if "FAKE-WRONG" in text
                else "correct"
            )
            result = json.dumps({"verdict": verdict, "reason": "fake judge"})
    elif any(s["status"] == "connected" for s in statuses):
        calls = 1
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "mcp__forge__assemble_context",
                            "input": {},
                        }
                    ]
                },
            }
        )
        envelope = {
            "legs": ["bm25", "vector"],
            "vector_leg": "configured",
            "rerank": "fallback" if "FALLBACK" in prompt else "off",
            "items": [],
        }
        emit(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": tool_failed,
                            "content": "Error: HTTP 502 Bad Gateway"
                            if tool_failed
                            else [{"type": "text", "text": json.dumps(envelope)}],
                        }
                    ]
                },
            }
        )
        wrong = "FAKE-WRONG" if "WRONGME" in prompt else "found it"
        result = "```json\n" + json.dumps({"answer": wrong, "sources": ["fake:1"]}) + "\n```"
    else:
        result = json.dumps({"answer": "I don't know", "sources": []})
    stop = None if model.startswith("claude-opus") else re.search(r"STOP:(\w+)", prompt)
    emit(
        {
            "type": "result",
            "subtype": stop.group(1) if stop else "success",
            "is_error": bool(stop),
            "result": result,
            "total_cost_usd": 0.01 + 0.01 * calls,
            "duration_ms": 1500 + 1000 * calls,
            "num_turns": 1 + calls,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 2000 + 1000 * calls,
                "cache_creation_input_tokens": 500,
            },
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
