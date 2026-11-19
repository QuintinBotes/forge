"""Facts about a diff (data only; the caller reads the diff, spec A §5.1).

The model lives in ``scf_contracts`` so every package can carry it.
:func:`signals_from_changes` turns the paths a diff touched, with their line
counts, into those facts. "Migration present" and "contract or schema touch" are
read from the paths alone, never from file contents.
"""

from __future__ import annotations

from collections.abc import Iterable

from scf_contracts import ChangeSignals

#: A directory with one of these names holds schema migrations.
_MIGRATION_DIRS = frozenset({"migrations", "alembic"})
#: A directory with one of these names holds contracts or schemas.
_CONTRACT_DIRS = frozenset({"contracts", "schemas", "proto"})
#: Interface-definition files, wherever they sit.
_CONTRACT_SUFFIXES = (".proto", ".graphql", ".avsc")


def _is_migration(path: str) -> bool:
    # Repository paths are always "/"-separated, whatever the host.
    return not _MIGRATION_DIRS.isdisjoint(path.split("/")[:-1])


def _is_contract(path: str) -> bool:
    *dirs, name = path.split("/")
    return (
        not _CONTRACT_DIRS.isdisjoint(dirs)
        or name.endswith(_CONTRACT_SUFFIXES)
        or name.startswith("openapi.")
    )


def signals_from_changes(changes: Iterable[tuple[str, int, int]]) -> ChangeSignals:
    """The facts of a diff from its ``(path, lines added, lines removed)`` entries."""
    changes = list(changes)
    paths = [path for path, _, _ in changes]
    return ChangeSignals(
        paths=paths,
        migration_present=any(_is_migration(path) for path in paths),
        touches_contracts=any(_is_contract(path) for path in paths),
        lines_added=sum(added for _, added, _ in changes),
        lines_removed=sum(removed for _, _, removed in changes),
    )


__all__ = ["ChangeSignals", "signals_from_changes"]
