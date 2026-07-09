"""External spec import (slice ``ss-import`` — track: Spec Studio).

Turns an existing spec authored *outside* Forge — a pasted/uploaded markdown
document (a GitHub issue, an RFC, a PRD) or a YAML manifest from another tool —
into a Forge ``spec.md`` draft, so a human can bring existing work into the SDD
lifecycle instead of retyping it.

Three tiers of effort, cheapest first:

1. **Direct parse** — the content is already a valid Forge ``spec.md``
   (:func:`forge_spec.parse_spec_md`) or ``manifest.yaml``
   (:func:`forge_spec.load_manifest`); it round-trips byte-for-byte in meaning.
2. **Normalize** — the content is YAML or markdown but uses looser shapes
   (``title``/``summary`` instead of ``name``, plain string lists instead of
   typed requirement objects, arbitrary heading names). Best-effort mapping
   onto :class:`~forge_contracts.SpecManifest`, assigning sequential ids
   (``R1``, ``A1``, ``Q1``, ...) where the source had none.
3. **Give up gracefully** — genuinely unparseable content (e.g. binary noise)
   is still returned verbatim as ``spec_md`` with ``parse_error`` set, mirroring
   ``ss-draft``'s graceful-failure contract, so the human can hand-fix it in the
   markdown editor rather than losing the paste.

This is draft-only, like ``POST /spec/draft``: nothing is persisted here — the
human reviews/refines the result and saves it via the normal spec-editing
endpoints (``PUT /spec/specs/{id}`` / ``/markdown`` / ``/manifest``).
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from forge_contracts import (
    AcceptanceCriterion,
    OpenQuestion,
    Requirement,
    SpecManifest,
)
from forge_contracts.enums import SpecStatus
from forge_spec import SpecParseError, load_manifest, parse_spec_md, render_spec_md

__all__ = [
    "IMPORT_PLACEHOLDER_ID",
    "SpecImport",
    "SpecImportFormat",
    "detect_format",
    "import_spec",
]

#: An imported spec has no real spec id yet (it is never persisted directly),
#: mirroring ``ss-draft``'s ``DRAFT_PLACEHOLDER_ID`` convention.
IMPORT_PLACEHOLDER_ID = "SPEC-IMPORT"

SpecImportFormat = Literal["markdown", "yaml"]

_REQUESTED_FORMATS = ("markdown", "yaml", "auto")


class SpecImport(BaseModel):
    """The draft-only result of ``POST /spec/import`` (nothing is persisted)."""

    source_format: SpecImportFormat
    spec_md: str
    #: The parsed/normalized preview, or ``None`` when the content did not
    #: parse or normalize (``parse_error`` then explains why).
    manifest: SpecManifest | None = None
    parse_error: str | None = None
    #: ``True`` when the source needed best-effort normalization (loose YAML
    #: keys, arbitrary markdown headings) rather than parsing directly as a
    #: canonical Forge ``spec.md`` / ``manifest.yaml``.
    normalized: bool = False


# --------------------------------------------------------------------------- #
# Format detection                                                            #
# --------------------------------------------------------------------------- #


def detect_format(content: str, requested: str = "auto") -> SpecImportFormat:
    """Resolve the source format: an explicit hint, or sniffed from ``content``.

    A canonical (or loosely-shaped) ``spec.md`` always has at least one
    Markdown ``#`` heading; genuine YAML never does, so heading detection is
    the deciding signal. Content that is neither valid YAML nor has headings
    still falls back to markdown (the more forgiving of the two normalizers).
    """
    if requested in ("markdown", "yaml"):
        return requested  # type: ignore[return-value]
    stripped = content.strip()
    if _HEADING_RE.search(stripped) is not None:
        return "markdown"
    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return "markdown"
    if isinstance(data, dict) and data:
        return "yaml"
    return "markdown"


# --------------------------------------------------------------------------- #
# Loose-YAML normalization                                                    #
# --------------------------------------------------------------------------- #

#: Alternate keys another tool might use for each manifest field, tried in order.
_YAML_NAME_KEYS = ("name", "title", "summary")
_YAML_REQUIREMENT_KEYS = ("requirements", "user_stories", "stories")
_YAML_ACCEPTANCE_KEYS = ("acceptance_criteria", "acceptance", "criteria")
_YAML_CONSTRAINT_KEYS = ("constraints", "non_functional_requirements", "nfrs")
_YAML_QUESTION_KEYS = ("open_questions", "questions")


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def _text_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("text", "description", "body", "summary"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return str(item)


def _coerce_requirements(raw: Any) -> list[Requirement]:
    if not isinstance(raw, list):
        return []
    out: list[Requirement] = []
    for i, item in enumerate(raw, start=1):
        rid = item.get("id") if isinstance(item, dict) else None
        out.append(Requirement(id=str(rid) if rid else f"R{i}", text=_text_of(item)))
    return out


def _coerce_acceptance(raw: Any, requirement_ids: list[str]) -> list[AcceptanceCriterion]:
    if not isinstance(raw, list):
        return []
    out: list[AcceptanceCriterion] = []
    for i, item in enumerate(raw, start=1):
        aid = item.get("id") if isinstance(item, dict) else None
        refs = item.get("req_refs") if isinstance(item, dict) else None
        out.append(
            AcceptanceCriterion(
                id=str(aid) if aid else f"A{i}",
                text=_text_of(item),
                req_refs=list(refs) if isinstance(refs, list) else requirement_ids,
            )
        )
    return out


def _coerce_open_questions(raw: Any) -> list[OpenQuestion]:
    if not isinstance(raw, list):
        return []
    out: list[OpenQuestion] = []
    for i, item in enumerate(raw, start=1):
        qid = item.get("id") if isinstance(item, dict) else None
        resolution = item.get("resolution") if isinstance(item, dict) else None
        out.append(
            OpenQuestion(
                id=str(qid) if qid else f"Q{i}",
                text=_text_of(item),
                resolution=resolution if isinstance(resolution, str) else None,
            )
        )
    return out


def _coerce_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [_text_of(item) for item in raw]


def _manifest_from_loose_yaml(data: dict[str, Any]) -> SpecManifest:
    """Best-effort map a loosely-shaped YAML mapping onto ``SpecManifest``."""
    name = _first_present(data, _YAML_NAME_KEYS) or "Imported spec"
    requirements = _coerce_requirements(_first_present(data, _YAML_REQUIREMENT_KEYS))
    acceptance = _coerce_acceptance(
        _first_present(data, _YAML_ACCEPTANCE_KEYS), [r.id for r in requirements]
    )
    constraints = _coerce_str_list(_first_present(data, _YAML_CONSTRAINT_KEYS))
    open_questions = _coerce_open_questions(_first_present(data, _YAML_QUESTION_KEYS))
    return SpecManifest(
        id=IMPORT_PLACEHOLDER_ID,
        name=str(name),
        status=SpecStatus.DRAFT,
        requirements=requirements,
        acceptance_criteria=acceptance,
        constraints=constraints,
        open_questions=open_questions,
    )


# --------------------------------------------------------------------------- #
# Loose-markdown normalization                                                #
# --------------------------------------------------------------------------- #

# Linear-time patterns: a greedy `(.+)` to end-of-line (no lazy `.+?` + trailing
# `\s*$` overlap, which CodeQL flags as polynomial/ReDoS on user-supplied text),
# with `[ \t]` separators so whitespace classes don't overlap the capture. The
# callers already `.strip()` the captured group, so trailing spaces are handled.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*][ \t]+(.+)$")

#: Heading text (lowercased, trailing ':' stripped) -> the bucket it feeds.
_SECTION_ALIASES: dict[str, str] = {
    "goal": "goal",
    "summary": "goal",
    "overview": "goal",
    "objective": "goal",
    "description": "goal",
    "requirements": "requirements",
    "functional requirements": "requirements",
    "user stories": "requirements",
    "acceptance criteria": "acceptance_criteria",
    "acceptance": "acceptance_criteria",
    "constraints": "constraints",
    "non-functional requirements": "constraints",
    "non functional requirements": "constraints",
    "open questions": "open_questions",
    "questions": "open_questions",
}


def _normalize_markdown(text: str) -> SpecManifest:
    """Best-effort map arbitrary markdown headings/bullets onto ``SpecManifest``."""
    buckets: dict[str, list[str]] = {
        "goal": [],
        "requirements": [],
        "acceptance_criteria": [],
        "constraints": [],
        "open_questions": [],
    }
    name: str | None = None
    current: str | None = None
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            level, title = heading.group(1), heading.group(2).strip().lower().rstrip(":")
            key = _SECTION_ALIASES.get(title)
            if key is not None:
                current = key
            elif level == "#" and name is None:
                name = heading.group(2).strip()
                current = None
            else:
                current = None
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        stripped = line.strip()
        if bullet is not None:
            buckets[current].append(bullet.group(1).strip())
        elif stripped:
            buckets[current].append(stripped)

    if name is None:
        if buckets["goal"]:
            name = buckets["goal"][0]
        else:
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            name = first_line[:200] or "Imported spec"

    requirements = [
        Requirement(id=f"R{i}", text=t) for i, t in enumerate(buckets["requirements"], 1)
    ]
    requirement_ids = [r.id for r in requirements]
    acceptance = [
        AcceptanceCriterion(id=f"A{i}", text=t, req_refs=requirement_ids)
        for i, t in enumerate(buckets["acceptance_criteria"], 1)
    ]
    open_questions = [
        OpenQuestion(id=f"Q{i}", text=t) for i, t in enumerate(buckets["open_questions"], 1)
    ]

    return SpecManifest(
        id=IMPORT_PLACEHOLDER_ID,
        name=name,
        status=SpecStatus.DRAFT,
        requirements=requirements,
        acceptance_criteria=acceptance,
        constraints=buckets["constraints"],
        open_questions=open_questions,
    )


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def import_spec(content: str, *, source_format: str = "auto") -> SpecImport:
    """Import ``content`` (an external markdown or YAML spec) as a draft.

    ``source_format`` is ``"markdown"``, ``"yaml"``, or ``"auto"`` (sniffed via
    :func:`detect_format`). Always returns a result — never raises — mirroring
    ``ss-draft``'s graceful-failure contract: unparseable content still comes
    back with the raw ``spec_md`` and a ``parse_error`` explaining why.
    """
    fmt = detect_format(content, source_format)

    if fmt == "yaml":
        try:
            manifest = load_manifest(content)
            return SpecImport(
                source_format="yaml",
                spec_md=render_spec_md(manifest),
                manifest=manifest,
                normalized=False,
            )
        except Exception:
            pass
        try:
            data = yaml.safe_load(content) or {}
            if not isinstance(data, dict):
                raise ValueError("YAML content must deserialize to a mapping")
            manifest = _manifest_from_loose_yaml(data)
            return SpecImport(
                source_format="yaml",
                spec_md=render_spec_md(manifest),
                manifest=manifest,
                normalized=True,
            )
        except Exception as exc:
            return SpecImport(source_format="yaml", spec_md=content, parse_error=str(exc))

    try:
        manifest = parse_spec_md(content)
        return SpecImport(
            source_format="markdown", spec_md=content, manifest=manifest, normalized=False
        )
    except SpecParseError:
        pass
    try:
        manifest = _normalize_markdown(content)
        return SpecImport(
            source_format="markdown",
            spec_md=render_spec_md(manifest),
            manifest=manifest,
            normalized=True,
        )
    except Exception as exc:
        return SpecImport(
            source_format="markdown", spec_md=content, parse_error=str(exc), normalized=True
        )


class SpecImportRequest(BaseModel):
    """Body for ``POST /spec/import``."""

    content: str = Field(min_length=1, description="The pasted/uploaded spec text.")
    source_format: Literal["markdown", "yaml", "auto"] = "auto"


__all__.append("SpecImportRequest")
