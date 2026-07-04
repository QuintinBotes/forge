"""Alembic revision-walk helpers (HARD-11).

Small, reusable functions the migration round-trip tests, CI, and ops scripts
share so the per-revision upgrade/downgrade walk is defined once. They read the
same ``ScriptDirectory`` Alembic itself uses, so they never drift from the real
version chain under ``packages/db/migrations/versions``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config
from alembic.script import ScriptDirectory

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "build_alembic_config",
    "iter_revisions",
    "revision_pairs",
]

_DB_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _DB_ROOT / "alembic.ini"
_MIGRATIONS = _DB_ROOT / "migrations"


def build_alembic_config(database_url: str) -> Config:
    """Return an Alembic :class:`Config` bound to ``database_url``.

    The ``script_location`` is pinned to the packaged migrations directory so the
    config works regardless of the caller's working directory.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def iter_revisions(cfg: Config) -> list[str]:
    """Return every revision id in ``base → head`` (application) order."""
    script = ScriptDirectory.from_config(cfg)
    # ``walk_revisions`` yields head → base; reverse for application order.
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def revision_pairs(cfg: Config) -> list[tuple[str, str | None]]:
    """Return ``(revision, down_revision)`` pairs in ``base → head`` order.

    ``down_revision`` is ``None`` for the baseline. A tuple whose ``down_revision``
    is a string names the exact single step that a per-revision downgrade walk
    reverses; ``None`` means "downgrade to ``base``".
    """
    script = ScriptDirectory.from_config(cfg)
    pairs: list[tuple[str, str | None]] = []
    for rev in reversed(list(script.walk_revisions())):
        down = rev.down_revision
        if isinstance(down, (tuple, list)):  # merge points (none today) → first parent
            down = down[0] if down else None
        pairs.append((rev.revision, down))
    return pairs


def head_revision(cfg: Config) -> str:
    """Return the single head revision id (raises if the chain is not linear)."""
    script = ScriptDirectory.from_config(cfg)
    heads: Sequence[str] = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected a single head revision, found {heads!r}")
    return heads[0]
