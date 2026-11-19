"""The dependency gate on an agent's diff (#187, #316, spec 003 FR-017, FR-018).

It compares each changed ``pyproject.toml``, pip requirements or constraints file,
``package.json``, and the ``uv.lock`` or ``package-lock.json`` beside a changed
manifest, before and after the change, and asks an injected :class:`RegistryClient`
about each dependency or locked version the change adds: one the registry does not
know fails, and so does a version younger than the policy's minimum age unless the
policy allows the package by name. A range is judged by the newest release it may
install (see :class:`RegistryClient`). A locked package is identified by the files
it downloads, not by its name. A package is local (not looked up) only when the base
branch takes it from the workspace or a directory in the repository (the root's
sources reach workspace members only), and the change leaves that source as it was.

It is a deny-list over the install inputs it names: what it cannot judge it does
not pass (``could_not_verify``): a manifest that does not parse or is not a regular
file, a requirement that does not come from the registry (a URL, a path or archive,
a VCS reference, an option line), an npm dist-tag or a range that may install a
pre-release, a change to a table that also decides what is installed or from where
(``[tool.uv]``, a source outside the registry and the repository, ``overrides``), a
lock entry whose files changed or are not its release's files on the registry, an
install input of another tool or ecosystem, a lockfile changed without its manifest
beside it, a lockfile whose versions it does not read, and a registry that errors
or times out (the worker's fault, in ``unavailable``). Network access is only in
the registry client.
"""

from __future__ import annotations

import codecs
import json
import posixpath
import re
import tomllib
from collections.abc import Collection
from datetime import datetime, timedelta
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import unquote

from scf_policy.diff_gate import GateFinding, GateResult, gate_result

__all__ = ["DEPENDENCY_CHECK", "Ecosystem", "RegistryClient", "check_dependencies", "is_manifest"]

#: The named check the dependency gate contributes to a run's check set.
DEPENDENCY_CHECK = "dependency_check"

Ecosystem = Literal["pypi", "npm"]


class RegistryClient(Protocol):
    def published(self, ecosystem: Ecosystem, name: str, version: str | None) -> datetime | None:
        """When ``version`` of ``name`` was published: on PyPI its newest file, as PyPI
        takes new files for an old release. With no version (a range), its newest
        release a range may install: on PyPI any release not yanked, as pip and uv
        install a pre-release when no final release matches; on npm a final release, as
        a range with no pre-release tag never installs one. A uv.lock entry is a pin.

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
        "environment.yaml",
        "conda-lock.yml",
        "hatch.toml",
        "pixi.toml",
        "pixi.lock",
        "pdm.toml",
        "poetry.toml",
        "uv.toml",
        "pip.conf",
        "pip.ini",
        "pnpm-workspace.yaml",
        ".pnpmfile.cjs",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "bunfig.toml",
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
        "deno.jsonc",
        "deno.lock",
    }
)
#: Other install inputs by pattern: PEP 751 lockfiles and pip-style locks (rye).
_OTHER_PATTERN = re.compile(r"pylock\.(?:[^/]+\.)?toml|.*(?:requirements|constraints).*\.lock")
_NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
#: Fields beside the dependency lists that also change what is installed or from where.
_NPM_OTHER = (
    "overrides",
    "resolutions",
    "pnpm",
    "bundleDependencies",
    "bundledDependencies",
    "workspaces",
    "packageManager",
)
_PYPROJECT_OTHER = ("poetry", "pdm", "hatch", "rye", "pixi")
_PYPI = "https://pypi.org/simple"
_PYPI_FILES = "https://files.pythonhosted.org/packages/"
_NPM = "https://registry.npmjs.org/"
#: What pip installs from the working tree when a requirement line ends with it, and
#: what a uv path source installs as a file (an archive nobody reviews), not a directory.
_ARCHIVES = (
    *(".zip", ".whl", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz"),
    *(".tar.xz", ".txz", ".tar.lz", ".tlz", ".tar.lzma", ".tar.zst", ".tzst"),
)
#: uv.lock sources that name a directory in the repository (``path`` is an archive).
_LOCAL_LOCK_SOURCES = {"editable", "virtual", "directory"}
#: Every install input by its case-folded name: a case-insensitive filesystem opens
#: ``.NPMRC`` as ``.npmrc``, so only the exact name of a parsed one is read.
_PARSED_NAMES = {"pyproject.toml", "package.json", *_LOCKFILES}
_FOLDED_NAMES = {n.casefold() for n in (*_OTHER_MANIFESTS, *_PARSED_NAMES)}
_PEP263 = re.compile(r"coding[:=]\s*([-\w.]+)", re.ASCII)
#: The keys of a ``[tool.uv.sources]`` entry that takes a package from the repository.
_LOCAL_SOURCE_KEYS = {"workspace", "path", "editable", "package"}
_NPM_NAME = re.compile(r"(?:@[\w-][\w.-]*/)?[\w-][\w.-]*")

_PEP508 = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)")
#: What may follow a name: version specifiers only, so a URL or path never parses.
_SPECIFIERS = re.compile(r"[\s(),<>=!~0-9A-Za-z.*+_-]*")
_PEP440_PIN = re.compile(r"\(?\s*===?\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*\)?")
_SEMVER_PIN = re.compile(r"=?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)")
#: One token of an npm range: a comparator, a hyphen or ``||``. A dist-tag is not one.
_NPM_RANGE_TOKEN = re.compile(
    r"(?:[<>]=?|[=~^])*(?:v?(?:\d+|[xX*])(?:\.(?:\d+|[xX*])){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)?|-|\|\|"
)
_WHEEL = re.compile(r"([^-]+)-([^-]+)-.+\.whl")
_SDIST = re.compile(r"(.+)-([^-]+)\.(?:tar\.gz|zip|tar\.bz2|tar\.xz|tgz)")

Dependency = tuple[Ecosystem, str, str | None]
#: An entry's identity, mapped to the text that finds its line, what to check (or why
#: it cannot be), and what it downloads (a lock entry's files; empty for a manifest).
Entries = dict[str, tuple[str, Dependency | str, str]]


def _parsed_kind(path: str) -> str | None:
    p = PurePosixPath(path)
    kinds = {"pyproject.toml": "pyproject", "package.json": "npm", "uv.lock": "uvlock"}
    if p.name in kinds or p.name in _READ_LOCKFILES:
        return kinds.get(p.name, "npmlock")
    name, parent = p.name.lower(), p.parent.name.lower()  # pip -r takes any name
    pip = "requirements" in name or "constraints" in name or parent == "requirements"
    return "requirements" if PurePosixPath(name).suffix in (".txt", ".in") and pip else None


def is_manifest(path: str) -> bool:
    """Whether the gate reads ``path``'s content (before and after the change)."""
    return _parsed_kind(path) is not None


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _inside(directory: str, path: Any) -> bool:
    """Whether ``path``, relative to ``directory``, is in the repository."""
    if not isinstance(path, str) or path.startswith(("/", "~")) or re.search(r"[$\\:]", path):
        return False  # absolute, home, environment, Windows or URL: not the repository's
    return posixpath.normpath(posixpath.join(directory, path)).split("/")[0] != ".."


def _local_source(source: Any, directory: str) -> bool:
    """A ``[tool.uv.sources]`` entry that takes a package from the workspace or the repo."""
    if not isinstance(source, dict) or not source.keys() <= _LOCAL_SOURCE_KEYS:
        return False
    if "path" in source:
        path = source["path"]
        return "workspace" not in source and _inside(directory, path) and not _archive(path)
    return source.get("workspace") is True


def _archive(path: str) -> bool:
    return re.sub(r"\[[^\]]*\]$", "", path).lower().endswith(_ARCHIVES)


def _uv_sources(text: str | None, directory: str) -> dict[str, tuple[Any, str]]:
    """``[tool.uv.sources]`` by normalized name, with the directory paths resolve from."""
    if not text:
        return {}
    sources = tomllib.loads(text).get("tool", {}).get("uv", {}).get("sources") or {}
    return {_normalized(n): (s, directory) for n, s in sources.items()}


def _python(requirement: str) -> Dependency | str:
    spec = requirement.split(";", 1)[0].strip()
    if _archive(spec):
        return "a local file, not a registry requirement"  # how pip reads `stream.zip`
    match = _PEP508.fullmatch(spec)
    if match is None or not _SPECIFIERS.fullmatch(match.group(2)):
        return "not a registry requirement"  # its text is not echoed: a URL may hold a token
    pin = _PEP440_PIN.fullmatch(match.group(2).strip())
    return "pypi", _normalized(match.group(1)), pin.group(1) if pin else None


def _npm(name: str, spec: Any) -> Dependency | str:
    if not isinstance(spec, str) or ":" in spec or "/" in spec:
        return f"{name} is not a registry version"
    if pin := _SEMVER_PIN.fullmatch(spec.strip()):
        return "npm", name, pin.group(1)
    tokens = spec.replace("||", " || ").split()
    if tokens in ([], ["*"]) or not all(_NPM_RANGE_TOKEN.fullmatch(t) for t in tokens):
        return f"{name}: a dist-tag or any version, which the gate cannot date"
    if re.search(r"[0-9xX*]-", spec.replace(" - ", " ")):
        return f"{name}: a range that may install a pre-release"
    return "npm", name, None


def _npm_lock(directory: str, key: str, entry: dict[str, Any]) -> tuple[str, Dependency | str]:
    """The package an npm lock entry installs, and what to check."""
    name = entry.get("name") or key.rsplit("node_modules/", 1)[1]
    version, resolved = entry.get("version"), entry.get("resolved")
    if entry.get("link"):
        return name, "" if _inside(directory, resolved) else f"{name} is not from the registry"
    if not (
        isinstance(version, str) and _SEMVER_PIN.fullmatch(version) and _NPM_NAME.fullmatch(name)
    ):
        return name, f"{name} is not from the registry"
    tarball = f"{_NPM}{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
    if resolved is not None and resolved != tarball:
        return name, f"{name} {version} is not that release's file on the registry"
    return name, ("npm", name, version)


def _pypi_file(file: Any, name: str, version: Any) -> bool:
    """Whether ``file`` (a uv.lock sdist or wheel) is ``name`` ``version``'s on PyPI."""
    url = file.get("url") if isinstance(file, dict) else None
    if not isinstance(url, str) or not url.startswith(_PYPI_FILES):
        return False
    filename = unquote(url.rsplit("/", 1)[1])
    match = _WHEEL.fullmatch(filename) or _SDIST.fullmatch(filename)
    return match is not None and _normalized(match[1]) == _normalized(name) and match[2] == version


def _entries(kind: str, text: str, origin: dict[str, str], directory: str) -> tuple[Entries, Any]:
    """The file's dependency entries, and the other tables that decide installs.

    ``origin`` marks the names a pyproject.toml takes from elsewhere (not looked up):
    ``local`` (the base branch's workspace or path source, unchanged) or ``elsewhere``
    (a source outside the registry and the repository).
    """
    if kind == "npm":
        data = json.loads(text) if text.strip() else {}
        deps = [(n, s) for sec in _NPM_SECTIONS for n, s in (data.get(sec) or {}).items()]
        # a workspace package is the repository's own: nothing comes from the registry
        deps = [(n, s) for n, s in deps if not re.fullmatch(r"workspace:[^/:]*", str(s))]
        entries = {json.dumps([n, s]): (json.dumps(n), _npm(n, s), "") for n, s in deps}
        return entries, [data.get(key) for key in _NPM_OTHER]
    entries = {}
    if kind == "npmlock":
        for key, e in (json.loads(text)["packages"] if text.strip() else {}).items():
            if "node_modules/" in key:
                name, dep = _npm_lock(directory, key, e)
                identity = json.dumps([key, name, e.get("version"), e.get("resolved")])
                if dep:  # a link inside the repository is local
                    entries[identity] = (json.dumps(key), dep, json.dumps(e.get("integrity")))
        return entries, None
    if kind == "uvlock":
        for package in tomllib.loads(text).get("package", []):
            name, version, source = package["name"], package.get("version"), package["source"]
            files = [f for f in [package.get("sdist"), *package.get("wheels", [])] if f]
            identity = json.dumps([name, version, source])
            if identity in entries:
                raise ValueError("a package is locked twice")
            if source.keys() == {"registry"} and source["registry"] == _PYPI:
                dep = ("pypi", _normalized(name), version)
                if not files or not all(_pypi_file(f, name, version) for f in files):
                    dep = f"{name} {version} is not that release's file on the registry"
            elif len(source) == 1 and source.keys() <= _LOCAL_LOCK_SOURCES:
                if _inside(directory, next(iter(source.values()))):
                    continue  # the workspace's own, or a path in the repository
                dep = f"{name} is not from the registry"
            else:
                dep = f"{name} is not from the registry"
            fingerprint = json.dumps([[f.get("url"), f.get("hash")] for f in files])
            entries[identity] = (f'name = "{name}"', dep, fingerprint)
        return entries, None
    if kind == "pyproject":
        data = tomllib.loads(text)
        project, tool = data.get("project", {}), data.get("tool", {})
        groups = [*project.get("optional-dependencies", {}).values()]
        groups += [*data.get("dependency-groups", {}).values()]
        specs = chain(
            project.get("dependencies", []),
            data.get("build-system", {}).get("requires", []),
            chain.from_iterable(groups),
        )
        for spec in (s for s in specs if isinstance(s, str)):
            dep = _python(spec)
            if isinstance(dep, tuple) and dep[1] in origin:
                if origin[dep[1]] == "local":
                    continue
                dep = f"{dep[1]} is not from the registry ([tool.uv.sources])"
            entries[spec] = (spec, dep, "")
        uv = tool.get("uv") or {}
        sources = uv.get("sources") or {}
        other = [
            {k: v for k, v in uv.items() if k != "sources"},
            {n: s for n, s in sources.items() if not _local_source(s, directory)},
            *(tool.get(k) for k in _PYPROJECT_OTHER),
            (tool.get("setuptools") or {}).get("dynamic"),
        ]
        return entries, other
    lines = (re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip() for line in text.splitlines())
    return {line: (line, _python(line), "") for line in lines if line}, None


def _origin(
    before: str, after: str, root: tuple[str | None, str | None], directory: str
) -> dict[str, str]:
    """Where a pyproject.toml's ``[tool.uv.sources]`` (over the root's, for a member of the
    base branch's workspace only: uv resolves any other project's names from PyPI) take
    packages from."""
    workspace = tomllib.loads(root[0] or "").get("tool", {}).get("uv", {}).get("workspace") or {}
    here = PurePosixPath(directory)
    member = not directory or (
        any(here.full_match(g) for g in workspace.get("members", []))
        and not any(here.full_match(g) for g in workspace.get("exclude", []))
    )
    was, now = (
        {**(_uv_sources(top, "") if member else {}), **_uv_sources(text, directory)}
        for text, top in ((before, root[0]), (after, root[1]))
    )
    origin = {}
    for name, (source, where) in now.items():
        if not _local_source(source, where):
            origin[name] = "elsewhere"
        elif was.get(name) == (source, where):
            origin[name] = "local"  # the base branch's, unchanged: the change cannot add one
    return origin


def _pip_decodes_otherwise(text: str) -> bool:
    """Whether pip may read a requirements file other than as the UTF-8 the gate read: it
    decodes with a BOM's codec or a PEP 263 ``coding:`` comment's on the first two lines
    (``utf-7`` can hide a line in a comment), and bytes not UTF-8 are replaced here."""
    if text.startswith("\ufeff") or "\ufffd" in text:
        return True
    for line in text.split("\n")[:2]:
        if line.startswith("#") and (declared := _PEP263.search(line)):
            try:
                return codecs.lookup(declared[1]).name not in ("utf-8", "ascii")
            except LookupError:
                return True
    return False


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
) -> GateResult:
    """Check the dependencies a change adds.

    ``changed`` is every path the change touches; ``manifests`` maps each one
    :func:`is_manifest` names to its text ``(before, after)``: empty when absent,
    ``None`` when it is not a regular file or could not be read. It also holds the
    root ``pyproject.toml`` (changed or not), whose ``[tool.uv.sources]`` every
    workspace member inherits.
    """
    findings: list[GateFinding] = []
    reasons: list[str] = []
    unavailable: list[str] = []
    parsed = []
    for path in changed:
        p = PurePosixPath(path)
        paired = p.name in _LOCKFILES and str(p.with_name(_LOCKFILES[p.name])) in changed
        folded = p.name.casefold()
        if (
            (folded in _FOLDED_NAMES and p.name not in _PARSED_NAMES)
            or _OTHER_PATTERN.fullmatch(folded)
            or (p.name in _LOCKFILES and not paired)
        ):
            reasons.append(f"{path}: the gate does not parse this manifest")
        elif p.name in _LOCKFILES and p.name not in _READ_LOCKFILES:
            reasons.append(f"{path}: the gate does not read its locked versions")
        elif is_manifest(path):
            parsed.append(path)
    allowed = {_normalized(n) for n in allowed_packages}
    root = manifests.get("pyproject.toml", ("", ""))
    for path in parsed:
        kind = _parsed_kind(path)
        assert kind is not None  # is_manifest
        before, after = manifests.get(path, (None, None))
        if before is None or after is None:
            reasons.append(f"{path}: not read as a regular file")
            continue
        if kind == "requirements" and _pip_decodes_otherwise(after):
            reasons.append(f"{path}: pip may decode it other than as UTF-8")
            continue
        directory = posixpath.dirname(path)
        try:
            origin = _origin(before, after, root, directory) if kind == "pyproject" else {}
            (old, old_other), (new, new_other) = (
                _entries(kind, before, origin, directory),
                _entries(kind, after, origin, directory),
            )
        except (ValueError, TypeError, AttributeError, LookupError) as exc:  # decode: ValueError
            reasons.append(f"{path}: does not parse ({type(exc).__name__})")
            continue
        if old_other != new_other:
            reasons.append(
                f"{path}: changes how or where packages are installed "
                "(a table the gate does not parse)"
            )
        for key, (needle, dependency, files) in new.items():
            if key in old and old[key][2] == files:
                continue
            line = _line_of(after, needle)
            if key in old:  # the same release, other files: what is downloaded changed
                reasons.append(f"{path}:{line}: the files it downloads changed")
                continue
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
