"""Prompt files of a ctxbench suite: load, hash, render, and read the agent's reply.

Placeholders are ``{name}`` and are filled in one pass, so a value that
itself contains ``{gold}`` (an agent's answer, say) is never expanded again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_eval.ctxbench.bank import PROMPT_FILES

__all__ = ["Prompts", "load_prompts", "parse_agent_reply", "prompt_hashes", "render"]

_PLACEHOLDER_RE = re.compile(r"\{(subject|question|gold|answerable|answer)\}")
_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*\n?|\n?```\s*$")


@dataclass(frozen=True)
class Prompts:
    agent: str
    judge: str


def load_prompts(suite_dir: Path) -> Prompts:
    text = {
        name: (suite_dir / "prompts" / name).read_text(encoding="utf-8") for name in PROMPT_FILES
    }
    return Prompts(agent=text["agent.md"], judge=text["judge.md"])


def prompt_hashes(suite_dir: Path) -> dict[str, str]:
    """sha256 of each prompt file, for the run record."""
    return {
        name: hashlib.sha256((suite_dir / "prompts" / name).read_bytes()).hexdigest()
        for name in PROMPT_FILES
    }


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill ``{subject}``, ``{question}``, ``{gold}``, ``{answerable}``, ``{answer}`` once."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def parse_agent_reply(text: str) -> tuple[str, list[str]]:
    """``(answer, sources)`` from the agent's reply, leniently.

    Fences are stripped and the first JSON object with an ``answer`` is
    taken; with none, the whole text is the answer and there are no sources.
    """
    body = _strip_fence(text)
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(body) if ch == "{"):
        try:
            obj: Any = decoder.raw_decode(body, start)[0]
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("answer"), str):
            sources = obj.get("sources")
            listed = [str(s) for s in sources] if isinstance(sources, list) else []
            return obj["answer"], listed
    return body, []
