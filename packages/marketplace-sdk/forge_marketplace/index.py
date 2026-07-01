"""Registry ``index.json`` parsing (fail-closed).

A registry publishes a single signed ``index.json`` (F32 §4). :func:`parse_index`
validates it against :class:`RegistryIndex` (``extra='forbid'``), so a malformed
index — an unexpected top-level key, a schema violation — is rejected outright
(AC3) and the caller records ``last_sync_status=error`` without upserting
anything.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from pydantic import ValidationError

from forge_marketplace.errors import RegistryFetchError
from forge_marketplace.models import RegistryIndex


def parse_index(raw: str | bytes | dict[str, Any]) -> RegistryIndex:
    """Parse + validate a registry index document, fail-closed.

    Accepts JSON/YAML text, raw bytes, or an already-parsed mapping. Raises
    :class:`RegistryFetchError` on any malformed / schema-invalid input.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise RegistryFetchError(f"registry index is not valid JSON/YAML: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise RegistryFetchError("registry index must be a JSON object")

    try:
        return RegistryIndex.model_validate(data)
    except ValidationError as exc:
        raise RegistryFetchError(f"registry index failed schema validation: {exc}") from exc


def dump_index(index: RegistryIndex) -> str:
    """Serialize a registry index to canonical JSON (author/registry-side)."""
    return json.dumps(index.model_dump(mode="json"), sort_keys=True, indent=2)


__all__ = ["dump_index", "parse_index"]
