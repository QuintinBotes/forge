"""Semver comparison + compatibility gate + latest/update resolution.

Pure functions the worker (`refresh_update_flags`) and the API service reuse to
answer: *which version is the latest non-yanked version compatible with the
running Forge?* and *does an installed version have a newer one?* (F32 AC13/14/15).

A tiny self-contained semver (MAJOR.MINOR.PATCH with an optional prerelease/build
suffix) avoids a runtime dependency on ``packaging``; prereleases sort **below**
their release (``1.2.0-rc.1 < 1.2.0``), matching semver.org ordering closely
enough for the marketplace's compatibility decisions.
"""

from __future__ import annotations

from functools import cmp_to_key

from forge_marketplace.models import RegistryIndexVersion


def parse_semver(version: str) -> tuple[int, int, int, tuple[object, ...]]:
    """Parse ``MAJOR.MINOR.PATCH[-prerelease][+build]`` into a comparable tuple.

    Build metadata (``+...``) is ignored for ordering (per semver). The
    prerelease component is returned as a tuple of dot-separated identifiers so
    numeric identifiers compare numerically and a release (no prerelease) sorts
    above any prerelease.
    """
    core, _, _build = version.partition("+")
    core, _, pre = core.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a MAJOR.MINOR.PATCH semver: {version!r}")
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"non-numeric semver core: {version!r}") from exc

    if not pre:
        # No prerelease => highest precedence: use a sentinel that sorts last.
        return major, minor, patch, ()
    identifiers: list[object] = []
    for ident in pre.split("."):
        identifiers.append(int(ident) if ident.isdigit() else ident)
    return major, minor, patch, tuple(identifiers)


def _cmp_pre(a: tuple[object, ...], b: tuple[object, ...]) -> int:
    """Compare prerelease identifier tuples. ``()`` (a release) is the greatest."""
    if a == b:
        return 0
    if not a:  # a is a release, b is a prerelease => a > b
        return 1
    if not b:
        return -1
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        # numeric < alphanumeric; else natural compare within the same type.
        x_num, y_num = isinstance(x, int), isinstance(y, int)
        if x_num and not y_num:
            return -1
        if y_num and not x_num:
            return 1
        return -1 if x < y else 1  # type: ignore[operator]
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_semver(a: str, b: str) -> int:
    """Return -1/0/1 for ``a`` <, ==, > ``b`` (semver precedence)."""
    am, ai, ap, apre = parse_semver(a)
    bm, bi, bp, bpre = parse_semver(b)
    if (am, ai, ap) != (bm, bi, bp):
        return -1 if (am, ai, ap) < (bm, bi, bp) else 1
    return _cmp_pre(apre, bpre)


def is_compatible(min_forge_version: str | None, forge_version: str) -> bool:
    """True iff ``forge_version`` satisfies ``min_forge_version`` (or none set)."""
    if not min_forge_version:
        return True
    return compare_semver(forge_version, min_forge_version) >= 0


def compatible_versions(
    versions: list[RegistryIndexVersion],
    *,
    forge_version: str,
    include_yanked: bool = False,
) -> list[RegistryIndexVersion]:
    """Filter to non-yanked (unless asked) versions the running Forge satisfies."""
    out: list[RegistryIndexVersion] = []
    for v in versions:
        if v.yanked and not include_yanked:
            continue
        if not is_compatible(v.min_forge_version, forge_version):
            continue
        out.append(v)
    return out


def sort_versions(versions: list[RegistryIndexVersion]) -> list[RegistryIndexVersion]:
    """Return ``versions`` sorted ascending by semver precedence."""
    return sorted(versions, key=cmp_to_key(lambda a, b: compare_semver(a.version, b.version)))


def latest_compatible(
    versions: list[RegistryIndexVersion],
    *,
    forge_version: str,
) -> RegistryIndexVersion | None:
    """Highest non-yanked version compatible with ``forge_version`` (or ``None``)."""
    eligible = compatible_versions(versions, forge_version=forge_version)
    if not eligible:
        return None
    return sort_versions(eligible)[-1]


def find_version(versions: list[RegistryIndexVersion], version: str) -> RegistryIndexVersion | None:
    """Return the index entry for an exact ``version`` string, or ``None``."""
    for v in versions:
        if v.version == version:
            return v
    return None


def has_newer_compatible(
    *,
    installed_version: str,
    versions: list[RegistryIndexVersion],
    forge_version: str,
) -> RegistryIndexVersion | None:
    """The newest compatible version strictly greater than ``installed_version``."""
    latest = latest_compatible(versions, forge_version=forge_version)
    if latest is None:
        return None
    if compare_semver(latest.version, installed_version) > 0:
        return latest
    return None


__all__ = [
    "compare_semver",
    "compatible_versions",
    "find_version",
    "has_newer_compatible",
    "is_compatible",
    "latest_compatible",
    "parse_semver",
    "sort_versions",
]
