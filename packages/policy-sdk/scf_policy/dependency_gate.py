"""The dependency gate on an agent's diff (#187, spec 003 FR-017, FR-018).

It compares each changed ``pyproject.toml``, ``requirements*.txt`` (or
``requirements/*.txt``) and ``package.json`` before and after the change, and asks
an injected :class:`RegistryClient` about each dependency the change adds: one the
registry does not know fails, and so does a version younger than the policy's
minimum age unless the policy allows the package by name. A range is judged by the
package's newest release, which the range may admit.

What the gate cannot judge it does not pass (``could_not_verify``): a registry that
errors or times out, a manifest that does not parse, a requirement that does not
come from the registry (a URL, a path, an option line), a change to a table that
also decides what is installed (``[tool.uv]``, ``overrides``), a manifest of another
ecosystem, and a lockfile changed with no manifest it pairs with. Network access is
only in the registry client.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Collection
from datetime import datetime, timedelta
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from scf_policy.diff_gate import GateFinding, GateResult, gate_result

__all__ = ["DEPENDENCY_CHECK", "Ecosystem", "RegistryClient", "check_dependencies", "is_manifest"]

#: The named check the dependency gate contributes to a run's check set.
DEPENDENCY_CHECK = "dependency_check"

Ecosystem = Literal["pypi", "npm"]


class RegistryClient(Protocol):
    def published(self, ecosystem: Ecosystem, name: str, version: str | None) -> datetime | None:
        """When ``version`` of ``name`` was published; with no version, its newest release.

        ``None`` when the registry knows no such package or version. Raises when it
        cannot answer.
        """
        ...


_LOCKFILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "Pipfile.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    }
)
_OTHER_MANIFESTS = frozenset(
    {
        "Pipfile",
        "setup.py",
        "setup.cfg",
        "environment.yml",
        "pnpm-workspace.yaml",
        ".npmrc",
        "pip.conf",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gradle.lockfile",
        "mix.exs",
        "mix.lock",
        "pubspec.yaml",
        "pubspec.lock",
        "Package.swift",
        "Package.resolved",
        "Podfile",
        "Podfile.lock",
        "deno.json",
        "deno.lock",
    }
)
_NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
#: Tables beside the dependency lists that also change what is installed or from where.
_NPM_OTHER = ("overrides", "resolutions", "pnpm", "bundleDependencies")
_PYPROJECT_OTHER = ("uv", "poetry", "pdm")

_PEP508 = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)")
_PEP440_PIN = re.compile(r"\(?\s*===?\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*\)?")
_SEMVER_PIN = re.compile(r"=?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)")


def _parsed_kind(path: str) -> Literal["pyproject", "requirements", "npm"] | None:
    p = PurePosixPath(path)
    if p.name == "pyproject.toml":
        return "pyproject"
    if p.name == "package.json":
        return "npm"
    if p.suffix == ".txt" and (
        p.name.startswith("requirements") or p.parent.name == "requirements"
    ):
        return "requirements"
    return None


def is_manifest(path: str) -> bool:
    """Whether the gate reads ``path``'s content (before and after the change)."""
    return _parsed_kind(path) is not None


def _entries(kind: str, text: str) -> tuple[list[tuple[str, str]], Any]:
    """The manifest's dependency entries, and the other tables that decide installs."""
    if kind == "npm":
        data = json.loads(text) if text.strip() else {}
        deps = [(n, s) for sec in _NPM_SECTIONS for n, s in (data.get(sec) or {}).items()]
        # a workspace package is the repository's own: nothing comes from the registry
        deps = [(n, s) for n, s in deps if not str(s).startswith("workspace:")]
        return deps, [data.get(key) for key in _NPM_OTHER]
    if kind == "pyproject":
        data = tomllib.loads(text)
        project = data.get("project", {})
        groups = [*project.get("optional-dependencies", {}).values()]
        groups += [*data.get("dependency-groups", {}).values()]
        specs = chain(
            project.get("dependencies", []),
            data.get("build-system", {}).get("requires", []),
            chain.from_iterable(groups),
        )
        tool = data.get("tool", {})
        return [(s, "") for s in specs if isinstance(s, str)], [
            tool.get(k) for k in _PYPROJECT_OTHER
        ]
    lines = (re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip() for line in text.split("\n"))
    return [(line, "") for line in lines if line], None


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency(kind: str, entry: tuple[str, str]) -> tuple[Ecosystem, str, str | None] | str:
    """``(ecosystem, name, exact version or None)``, or why the entry cannot be checked."""
    if kind == "npm":
        name, spec = entry
        if not isinstance(spec, str) or ":" in spec or "/" in spec:
            return f"{name} is not a registry version"
        pin = _SEMVER_PIN.fullmatch(spec.strip())
        return "npm", name, pin.group(1) if pin else None
    requirement = entry[0]
    match = _PEP508.fullmatch(requirement.split(";", 1)[0].strip())
    if match is None or requirement.endswith("\\") or match.group(2).startswith("@"):
        return "not a registry requirement"  # its text is not echoed: a URL may hold a token
    pin = _PEP440_PIN.fullmatch(match.group(2).strip())
    return "pypi", _normalized(match.group(1)), pin.group(1) if pin else None


def _line_of(text: str, needle: str) -> int:
    lines = text.split("\n")
    return next((n for n, line in enumerate(lines, start=1) if needle in line), 0)


def check_dependencies(
    changed: Collection[str],
    manifests: dict[str, tuple[str, str]],
    registry: RegistryClient,
    *,
    min_age_days: int,
    allowed_packages: Collection[str],
    now: datetime,
) -> GateResult:
    """Check the dependencies a change adds.

    ``changed`` is every path the change touches; ``manifests`` maps each one
    :func:`is_manifest` names to its text ``(before, after)`` (empty when absent).
    """
    findings: list[GateFinding] = []
    reasons: list[str] = []
    parsed = [p for p in changed if is_manifest(p)]
    for path in changed:
        name = PurePosixPath(path).name
        if name in _OTHER_MANIFESTS or (name in _LOCKFILES and not parsed):
            reasons.append(f"{path}: the gate does not parse this manifest")
    allowed = {_normalized(n) for n in allowed_packages}
    for path in parsed:
        kind = _parsed_kind(path)
        assert kind is not None  # is_manifest
        before, after = manifests.get(path, ("", ""))
        try:
            (old, old_other), (new, new_other) = _entries(kind, before), _entries(kind, after)
        except (ValueError, TypeError, AttributeError) as exc:  # the decode errors are ValueErrors
            reasons.append(f"{path}: does not parse ({type(exc).__name__})")
            continue
        if old_other != new_other:
            reasons.append(f"{path}: changes a table the gate does not parse")
        for entry in (e for e in new if e not in old):
            line = _line_of(after, json.dumps(entry[0]) if kind == "npm" else entry[0])
            dependency = _dependency(kind, entry)
            if isinstance(dependency, str):
                reasons.append(f"{path}:{line}: {dependency}")
                continue
            ecosystem, package, version = dependency
            release = f"{package} {version or '(newest release)'}"
            try:
                published = registry.published(ecosystem, package, version)
            except Exception as exc:  # an error or a timeout: the gate cannot verify
                reasons.append(f"{path}:{line}: {ecosystem} did not answer ({type(exc).__name__})")
                continue
            if published is None:
                message = f"{ecosystem} has no {release}"
                findings.append(
                    GateFinding(file=path, line=line, rule="unknown-dependency", message=message)
                )
            elif (
                now - published < timedelta(days=min_age_days)
                and _normalized(package) not in allowed
            ):
                message = (
                    f"{release} was published {published:%Y-%m-%d}, "
                    f"under the policy's {min_age_days} days"
                )
                findings.append(
                    GateFinding(file=path, line=line, rule="dependency-too-new", message=message)
                )
    return gate_result(findings, reasons=reasons)
