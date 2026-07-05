"""Canonical, field-scoped content hashing for echo suppression + conflict detect.

The hash covers **only** the fields F18 actually syncs (title, description,
status *category*, priority, assignee email, labels). Volatile/transport-only
fields (``version``, ``updated_at``, the provider ``raw`` blob, ids, urls) are
deliberately excluded so:

* a no-op re-sync is detected (``forge_content_hash`` unchanged -> ``no_change``);
* an echo write is suppressed (a re-delivered webhook hashes identically);
* provider secrets never feed the digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from forge_contracts.pm import ExternalTask, ForgePriority, ForgeTask, StatusCategory


def _digest(parts: dict[str, object]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _norm_labels(labels: Iterable[str]) -> list[str]:
    return sorted({label.strip() for label in labels if label and label.strip()})


def _norm_text(value: str | None) -> str:
    return (value or "").strip()


def forge_content_hash(task: ForgeTask) -> str:
    """Stable digest of the Forge-side synced subset (excludes version/updated_at)."""
    category = (
        task.status_category.value
        if isinstance(task.status_category, StatusCategory)
        else str(task.status_category)
    )
    priority = (
        task.priority.value if isinstance(task.priority, ForgePriority) else str(task.priority)
    )
    return _digest(
        {
            "title": _norm_text(task.title),
            "description": _norm_text(task.description_md),
            "status_category": category,
            "priority": priority,
            "assignee_email": _norm_text(task.assignee_email).lower(),
            "labels": _norm_labels(task.label_names),
        }
    )


def external_content_hash(task: ExternalTask) -> str:
    """Stable digest of the external-side synced subset (excludes raw/updated_at)."""
    category = (
        task.status_category.value
        if isinstance(task.status_category, StatusCategory)
        else (task.status_category or "")
    )
    return _digest(
        {
            "title": _norm_text(task.title),
            "description": _norm_text(task.description_md),
            "status_category": category,
            "priority": _norm_text(task.priority_token).lower(),
            "assignee_email": _norm_text(task.assignee_email).lower(),
            "labels": _norm_labels(task.labels),
        }
    )


__all__ = ["external_content_hash", "forge_content_hash"]
