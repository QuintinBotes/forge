"""Discover locally installed agent CLIs. Never raises; absence is not an error."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DEFAULT_PREFERENCE", "CliName", "DetectedCli", "detect_clis", "parse_preference"]


class CliName(StrEnum):
    claude = "claude"
    codex = "codex"


#: Order tried when the operator expresses no preference.
DEFAULT_PREFERENCE: tuple[CliName, ...] = (CliName.claude, CliName.codex)


@dataclass(frozen=True)
class DetectedCli:
    name: CliName
    path: str


def detect_clis(
    *,
    preference: Sequence[CliName] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[DetectedCli]:
    """Return installed CLIs in ``preference`` order. Empty on any failure."""
    order = tuple(preference) if preference else DEFAULT_PREFERENCE
    found: list[DetectedCli] = []
    for name in order:
        try:
            path = which(str(name))
        except Exception:
            continue
        if path:
            found.append(DetectedCli(name=name, path=path))
    return found


def parse_preference(raw: str | None) -> tuple[CliName, ...] | None:
    """Parse ``FORGE_LOCAL_CLI_PREFERENCE`` (e.g. ``"codex,claude"``).

    Returns ``None`` when the variable is unset/blank or names nothing
    recognisable, which every caller reads as "no preference expressed" and
    falls back to :data:`DEFAULT_PREFERENCE`. Unknown names are skipped rather
    than raising: this parses operator-supplied environment, and a typo must
    not take a process down.

    Shared by ``ModelClientConfig.from_env`` (worker/API side, where the
    preference decides the run's label and which CLI a direct subprocess
    client drives) and ``forge_cli_bridge`` (host side, where it decides which
    backend the bridge actually spawns). One parser so the two sides cannot
    disagree about what a given value means.
    """
    if not raw or not raw.strip():
        return None
    names: list[CliName] = []
    for part in raw.split(","):
        try:
            names.append(CliName(part.strip().lower()))
        except ValueError:
            continue
    return tuple(names) or None
