"""Identity recall channel 1: exact and suffix-stripped name matching.

Pure module — no I/O. A name matches another when their normalised forms are
equal, or when stripping trailing corporate suffixes (and punctuation) makes
them equal, restricted to compatible types: same class, ancestor/descendant,
or either untyped. Classes declared disjoint never match.
"""

from __future__ import annotations

from collections.abc import Mapping

from forge_world.ledger import ClassDef

#: Trailing corporate suffixes stripped for recall (spec list, verbatim).
NAME_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "ltd",
        "llc",
        "gmbh",
        "corp",
        "corporation",
        "company",
        "co",
        "plc",
        "ag",
        "sa",
        "bv",
    }
)

_TRAILING_PUNCT = ".,;:!?"


def normalise_name(name: str) -> str:
    """Casefold, collapse whitespace, and drop trailing punctuation."""
    normalised = " ".join(name.casefold().split())
    return normalised.strip().rstrip(_TRAILING_PUNCT).strip()


def strip_suffixes(name: str) -> str:
    """Normalise ``name`` and remove trailing corporate-suffix tokens.

    "Acme, Inc." -> "acme"; repeated suffixes ("Acme Corp Inc") all go. A
    name made only of suffixes keeps its last token rather than vanishing.
    """
    tokens = normalise_name(name).replace(",", " ").split()
    while len(tokens) > 1 and tokens[-1].rstrip(_TRAILING_PUNCT) in NAME_SUFFIXES:
        tokens.pop()
    return " ".join(token.rstrip(_TRAILING_PUNCT) for token in tokens).strip()


def names_match(a: str, b: str) -> bool:
    """True iff names are equal exactly or after suffix stripping."""
    if not a.strip() or not b.strip():
        return False
    if normalise_name(a) == normalise_name(b):
        return True
    stripped_a, stripped_b = strip_suffixes(a), strip_suffixes(b)
    return bool(stripped_a) and stripped_a == stripped_b


def _ancestors(key: str, classes: Mapping[str, ClassDef]) -> set[str]:
    """``key`` plus its ``parent_key`` chain (cycle-safe)."""
    seen: set[str] = set()
    current: str | None = key
    while current is not None and current not in seen:
        seen.add(current)
        entry = classes.get(current)
        current = entry.parent_key if entry is not None else None
    return seen


def classes_disjoint(a: str, b: str, classes: Mapping[str, ClassDef]) -> bool:
    """True iff ``a`` and ``b`` (or their ancestors) are declared disjoint."""
    if a == b:
        return False
    a_chain, b_chain = _ancestors(a, classes), _ancestors(b, classes)
    for key in a_chain:
        entry = classes.get(key)
        if entry is not None and b_chain & set(entry.disjoint_with):
            return True
    for key in b_chain:
        entry = classes.get(key)
        if entry is not None and a_chain & set(entry.disjoint_with):
            return True
    return False


def types_compatible(a: str | None, b: str | None, classes: Mapping[str, ClassDef]) -> bool:
    """True iff two entity types may denote the same thing.

    Either untyped is compatible; declared disjointness never is; otherwise
    only the same class or an ancestor/descendant pair qualifies.
    """
    if a is None or b is None:
        return True
    if classes_disjoint(a, b, classes):
        return False
    return a == b or a in _ancestors(b, classes) or b in _ancestors(a, classes)


__all__ = [
    "NAME_SUFFIXES",
    "classes_disjoint",
    "names_match",
    "normalise_name",
    "strip_suffixes",
    "types_compatible",
]
