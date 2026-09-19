"""Discover locally installed agent CLIs. Never raises; absence is not an error."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["CliName", "DetectedCli", "DEFAULT_PREFERENCE", "detect_clis"]


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
        except Exception:  # noqa: BLE001 - detection must never break resolution
            continue
        if path:
            found.append(DetectedCli(name=name, path=path))
    return found
