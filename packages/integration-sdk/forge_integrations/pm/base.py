"""Shared adapter base: pure value mapping with per-connection overrides.

Concrete adapters (Jira, Linear) supply provider-specific default tables and the
external I/O; the mapping algorithm (override precedence, case-insensitive
fallback, *never silently drop* -> :class:`MappingError`) lives here so every
adapter behaves identically — the OSS extension-point promise.
"""

from __future__ import annotations

from typing import ClassVar

from forge_contracts.enums import Direction
from forge_contracts.pm import PMProvider
from forge_integrations.pm.errors import MappingError


def resolve_value(
    value: str,
    *,
    table: dict[str, str],
    override: dict[str, str] | None,
    kind: str,
) -> str:
    """Resolve ``value`` through ``override`` then ``table`` (case-insensitive).

    Raises :class:`MappingError` when no mapping exists (never returns a guess).
    """
    merged: dict[str, str] = {**table, **(override or {})}
    if value in merged:
        return merged[value]
    lowered = {str(k).lower(): v for k, v in merged.items()}
    key = str(value).lower()
    if key in lowered:
        return lowered[key]
    raise MappingError(f"cannot map {kind} {value!r}")


def _invert(mapping: dict[str, str] | None) -> dict[str, str]:
    return {v: k for k, v in (mapping or {}).items()}


class BaseAdapter:
    """Base implementing the pure mapping half of the ``PMAdapter`` Protocol.

    Subclasses set ``provider`` and the four class-level default tables, and
    implement the async external-I/O + webhook methods.
    """

    provider: PMProvider

    # forge category -> external status value (OUT)
    status_out_table: ClassVar[dict[str, str]] = {}
    # external status value -> forge category (IN)
    status_in_table: ClassVar[dict[str, str]] = {}
    # forge priority -> external priority token (OUT)
    priority_out_table: ClassVar[dict[str, str]] = {}
    # external priority token -> forge priority (IN)
    priority_in_table: ClassVar[dict[str, str]] = {}
    # external field name -> forge field name (IN)
    field_in_table: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        *,
        status_map: dict[str, str] | None = None,
        priority_map: dict[str, str] | None = None,
        field_map: dict[str, str] | None = None,
    ) -> None:
        # Overrides are stored as {forge_value: external_value}.
        self.status_map = dict(status_map or {})
        self.priority_map = dict(priority_map or {})
        self.field_map = dict(field_map or {})

    # ------------------------------------------------------------------ #
    # mapping (pure)                                                      #
    # ------------------------------------------------------------------ #

    def map_status(self, value: str, direction: Direction) -> str:
        if direction == Direction.OUT:
            return resolve_value(
                value, table=self.status_out_table, override=self.status_map, kind="status"
            )
        return resolve_value(
            value,
            table=self.status_in_table,
            override=_invert(self.status_map),
            kind="status",
        )

    def map_priority(self, value: str, direction: Direction) -> str:
        if direction == Direction.OUT:
            return resolve_value(
                value,
                table=self.priority_out_table,
                override=self.priority_map,
                kind="priority",
            )
        return resolve_value(
            value,
            table=self.priority_in_table,
            override=_invert(self.priority_map),
            kind="priority",
        )

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        if direction == Direction.IN:
            mapping = {**self.field_in_table, **_invert(self.field_map)}
        else:
            mapping = {**_invert(self.field_in_table), **self.field_map}
        return {mapping.get(k, k): v for k, v in fields.items()}


__all__ = ["BaseAdapter", "resolve_value"]
