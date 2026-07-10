"""Dotted-path JSON get/set helpers shared by the generic adapter + webhooks.

A BYO board can shape its JSON payloads however it likes, so the generic
connector addresses individual values by a small dotted-path language
(``"data.fields.name"``) instead of assuming a fixed shape. An empty path
means "the root value itself". List indices are supported (``"items.0.id"``)
but list *searching* is not — a board whose shape needs that is a native-
adapter candidate, not a generic-config one.
"""

from __future__ import annotations

from typing import Any


def get_path(obj: Any, path: str) -> Any:
    """Return the value at dotted ``path`` in ``obj``, or ``None`` if absent."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def set_path(obj: dict, path: str, value: Any) -> None:
    """Set ``value`` at dotted ``path`` inside ``obj`` (dicts), creating nesting."""
    if not path:
        raise ValueError("set_path requires a non-empty path")
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


__all__ = ["get_path", "set_path"]
