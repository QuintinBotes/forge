"""The dependency gate on an agent's diff (#187, spec 003 FR-017, FR-018).

It compares each changed ``pyproject.toml``, pip requirements or constraints file,
``package.json``, and the ``uv.lock`` or ``package-lock.json`` beside a changed
manifest, before and after the change, and asks an injected :class:`RegistryClient`
about each dependency or locked version the change adds: one the registry does not
know fails, and so does a version younger than the policy's minimum age unless the
policy allows the package by name. A range is judged by the package's newest final
release, which the range may admit. A workspace or path package is local.

What the gate cannot judge it does not pass (``could_not_verify``): a manifest that
does not parse or could not be read, a requirement that does not come from the
registry (a URL, a path, a VCS reference, an option line), a change to a table that
also decides what is installed (``[tool.uv]``, ``[tool.hatch]``, ``overrides``), a
manifest of another ecosystem or an installer's config, a lockfile changed without
its manifest beside it, a lockfile whose versions it does not read, and a registry
that errors or times out (the worker's fault, in ``unavailable``). Network access is
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

__all__ = [
    "DEPENDENCY_CHECK",
    "Ecosystem",
    "RegistryClient",
    "check_dependencies",
    "is_manifest",
    "workspace_packages",
]

#: The named check the dependency gate contributes to a run's check set.
DEPENDENCY_CHECK = "dependency_check"

Ecosystem = Literal["pypi", "npm"]


class RegistryClient(Protocol):
    def published(self, ecosystem: Ecosystem, name: str, version: str | None) -> datetime | None:
        """When ``version`` of ``name`` was published; with no version, its newest final
        (not pre-release, not yanked) release.

        ``None`` when the registry knows no such package or version. Raises when it
        cannot answer.
        """
        ...


#: Each lockfile, by the manifest it pairs with in its own directory.
_LOCKFILES = {
    "uv.lock": "pyproject.toml",
    "poetry.lock": "pyproject.toml",
    "pdm.lock": "pyproject.toml",
    "package-lock.json": "package.json",
    "npm-shrinkwrap.json": "package.json",
    "pnpm-lock.yaml": "package.json",
    "yarn.lock": "package.json",
    "bun.lock": "package.json",
    "bun.lockb": "package.json",
}
#: The lockfiles whose locked versions the gate reads.
_READ_LOCKFILES = ("uv.lock", "package-lock.json", "npm-shrinkwrap.json")
_OTHER_MANIFESTS = frozenset(
    {
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
        "environment.yml",
        "uv.toml",
        "pip.conf",
        "pip.ini",
        "pnpm-workspace.yaml",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
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
_PYPROJECT_OTHER = ("poetry", "pdm", "hatch", "rye", "pixi")
_PYPI = "https://pypi.org/simple"
_NPM = "https://registry.npmjs.org/"

_PEP508 = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)")
#: What may follow a name: version specifiers only, so a URL or path never parses.
_SPECIFIERS = re.compile(r"[\s(),<>=!~0-9A-Za-z.*+_-]*")
_PEP440_PIN = re.compile(r"\(?\s*===?\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*\)?")
_SEMVER_PIN = re.compile(r"=?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)")

Dependency = tuple[Ecosystem, str, str | None]
#: An entry's identity, the text that finds its line, and what to check (or why not).
Entries = dict[str, tuple[str, Dependency | str]]


def _parsed_kind(path: str) -> str | None:
    p = PurePosixPath(path)
    kinds = {"pyproject.toml": "pyproject", "package.json": "npm", "uv.lock": "uvlock"}
    if p.name in kinds or p.name in _READ_LOCKFILES:
        return kinds.get(p.name, "npmlock")
    pip = "requirements" in p.name or "constraints" in p.name or p.parent.name == "requirements"
    return "requirements" if p.suffix in (".txt", ".in") and pip else None


def is_manifest(path: str) -> bool:
    """Whether the gate reads ``path``'s content (before and after the change)."""
    return _parsed_kind(path) is not None


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python(requirement: str) -> Dependency | str:
    match = _PEP508.fullmatch(requirement.split(";", 1)[0].strip())
    if match is None or not _SPECIFIERS.fullmatch(match.group(2)):
        return "not a registry requirement"  # its text is not echoed: a URL may hold a token
    pin = _PEP440_PIN.fullmatch(match.group(2).strip())
    return "pypi", _normalized(match.group(1)), pin.group(1) if pin else None


def _npm(name: str, spec: Any) -> Dependency | str:
    if not isinstance(spec, str) or ":" in spec or "/" in spec:
        return f"{name} is not a registry version"
    pin = _SEMVER_PIN.fullmatch(spec.strip())
    return "npm", name, pin.group(1) if pin else None


def workspace_packages(pyproject: str) -> set[str]:
    """The packages ``[tool.uv.sources]`` takes from the workspace or a path."""
    sources = tomllib.loads(pyproject).get("tool", {}).get("uv", {}).get("sources") or {}
    local = (
        n for n, s in sources.items() if isinstance(s, dict) and {"workspace", "path"} & s.keys()
    )
    return {_normalized(n) for n in local}


def _entries(kind: str, text: str, local: set[str]) -> tuple[Entries, Any]:
    """The manifest's dependency entries, and the other tables that decide installs."""
    if kind == "npm":
        data = json.loads(text) if text.strip() else {}
        deps = [(n, s) for sec in _NPM_SECTIONS for n, s in (data.get(sec) or {}).items()]
        # a workspace package is the repository's own: nothing comes from the registry
        deps = [(n, s) for n, s in deps if not str(s).startswith("workspace:")]
        entries = {f"{n}\0{s}": (json.dumps(n), _npm(n, s)) for n, s in deps}
        return entries, [data.get(key) for key in _NPM_OTHER]
    if kind == "npmlock":
        entries = {}
        for key, e in (json.loads(text)["packages"] if text.strip() else {}).items():
            if "node_modules/" in key and not e.get("link"):
                name = e.get("name") or key.rsplit("node_modules/", 1)[1]
                resolved = e.get("resolved")
                dep: Dependency | str = ("npm", name, e.get("version"))
                if resolved and not resolved.startswith(_NPM):
                    dep = f"{name} is not from the registry"
                entries[json.dumps([key, e.get("version"), resolved])] = (json.dumps(key), dep)
        return entries, None
    if kind == "uvlock":
        entries = {}
        for package in tomllib.loads(text).get("package", []):
            name, source = package["name"], package.get("source", {})
            if source and source.keys() <= {"editable", "virtual", "directory", "path"}:
                continue  # the workspace's own, or a path
            dep = ("pypi", _normalized(name), package.get("version"))
            if source.get("registry") != _PYPI:
                dep = f"{name} is not from the registry"
            entries[json.dumps([name, package.get("version"), source])] = (f'name = "{name}"', dep)
        return entries, None
    if kind == "pyproject":
        data = tomllib.loads(text)
        project, tool = data.get("project", {}), data.get("tool", {})
        local = local | workspace_packages(text)
        groups = [*project.get("optional-dependencies", {}).values()]
        groups += [*data.get("dependency-groups", {}).values()]
        specs = chain(
            project.get("dependencies", []),
            data.get("build-system", {}).get("requires", []),
            chain.from_iterable(groups),
        )
        resolved = {s: (s, _python(s)) for s in specs if isinstance(s, str)}
        entries = {
            s: e for s, e in resolved.items() if isinstance(e[1], str) or e[1][1] not in local
        }
        uv = tool.get("uv") or {}
        other = [
            {k: v for k, v in uv.items() if k not in ("sources", "workspace")},
            {n: s for n, s in (uv.get("sources") or {}).items() if _normalized(n) not in local},
            *(tool.get(k) for k in _PYPROJECT_OTHER),
            (tool.get("setuptools") or {}).get("dynamic"),
        ]
        return entries, other
    lines = (re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip() for line in text.split("\n"))
    return {line: (line, _python(line)) for line in lines if line}, None


def _line_of(text: str, needle: str) -> int:
    """The line that is ``needle``, else the first that holds it, else 0."""
    lines = list(enumerate(text.split("\n"), start=1))
    exact = next((n for n, line in lines if line == needle), 0)
    return exact or next((n for n, line in lines if needle in line), 0)


def check_dependencies(
    changed: Collection[str],
    manifests: dict[str, tuple[str | None, str | None]],
    registry: RegistryClient,
    *,
    min_age_days: int,
    allowed_packages: Collection[str],
    now: datetime,
    local_packages: Collection[str] = (),
) -> GateResult:
    """Check the dependencies a change adds.

    ``changed`` is every path the change touches; ``manifests`` maps each one
    :func:`is_manifest` names to its text ``(before, after)``: empty when absent,
    ``None`` when it could not be read. ``local_packages`` names packages the
    workspace provides (the root's ``[tool.uv.sources]``).
    """
    findings: list[GateFinding] = []
    reasons: list[str] = []
    unavailable: list[str] = []
    parsed = []
    for path in changed:
        p = PurePosixPath(path)
        paired = p.name in _LOCKFILES and str(p.with_name(_LOCKFILES[p.name])) in changed
        if p.name in _OTHER_MANIFESTS or (p.name in _LOCKFILES and not paired):
            reasons.append(f"{path}: the gate does not parse this manifest")
        elif p.name in _LOCKFILES and p.name not in _READ_LOCKFILES:
            reasons.append(f"{path}: the gate does not read its locked versions")
        elif is_manifest(path):
            parsed.append(path)
    allowed = {_normalized(n) for n in allowed_packages}
    local = {_normalized(n) for n in local_packages}
    for path in parsed:
        kind = _parsed_kind(path)
        assert kind is not None  # is_manifest
        before, after = manifests.get(path, ("", ""))
        if before is None or after is None:
            reasons.append(f"{path}: not read")
            continue
        try:
            (old, old_other), (new, new_other) = (
                _entries(kind, before, local),
                _entries(kind, after, local),
            )
        except (ValueError, TypeError, AttributeError, LookupError) as exc:  # decode: ValueError
            reasons.append(f"{path}: does not parse ({type(exc).__name__})")
            continue
        if old_other != new_other:
            reasons.append(f"{path}: changes a table the gate does not parse")
        for needle, dependency in (e for key, e in new.items() if key not in old):
            line = _line_of(after, needle)
            if isinstance(dependency, str):
                reasons.append(f"{path}:{line}: {dependency}")
                continue
            ecosystem, package, version = dependency
            release = f"{package} {version or '(newest release)'}"
            try:
                published = registry.published(ecosystem, package, version)
            except Exception as exc:  # an error or a timeout: the worker cannot verify
                unavailable.append(
                    f"{path}:{line}: {ecosystem} did not answer ({type(exc).__name__})"
                )
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
    return gate_result(findings, reasons=reasons, unavailable=unavailable)
