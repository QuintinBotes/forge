"""Dual-format ``spec.md`` (de)serialization for the spec engine.

``spec.md`` is the human/agent prose surface for a spec; ``manifest.yaml`` is the
precise machine surface. Both are *canonical, non-lossy* serializations of the
one :class:`~forge_contracts.SpecManifest` DTO: editing either updates the model
and re-renders the other.

This module owns the ``spec.md`` side of that contract:

- :func:`render_spec_md` — ``SpecManifest`` -> markdown text.
- :func:`parse_spec_md` — markdown text -> ``SpecManifest`` (the exact inverse).
- :class:`SpecParseError` — a line-anchored parse failure.

Document shape (a YAML frontmatter block for scalar/list *metadata*, then
``##`` sections for the typed lists)::

    ---
    id: SPEC-1
    status: draft
    constitution_refs: []
    repos: []
    execution_mode: single_agent
    skill_profile: null
    plan_ref: null
    tasks_ref: null
    validation_ref: null
    ---

    ## Goal

    <name>

    ## Requirements

    - **R1**: <text>

    ## Acceptance Criteria

    - **A1** (R1): <text>

    ## Constraints

    - <constraint>

    ## Open Questions

    - **Q1**: <text>
      - Resolution: <resolution>

    ## Decisions

    ### ADR-1 — <title>

    - Status: accepted
    - Context: <context>
    - Decision: <decision>
    - Consequences: <consequences>

``render_spec_md(parse_spec_md(...))`` and ``parse_spec_md(render_spec_md(...))``
both round-trip, and a spec.md and its manifest.yaml parse to the *same*
``SpecManifest`` (cross-format consistency).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from forge_contracts import (
    ADR,
    AcceptanceCriterion,
    ForgeError,
    OpenQuestion,
    Requirement,
    SpecManifest,
)

# --------------------------------------------------------------------------- #
# Error                                                                        #
# --------------------------------------------------------------------------- #


class SpecParseError(ForgeError, ValueError):
    """A ``spec.md`` document could not be parsed.

    ``line`` is the 1-based source line the failure anchors to (``None`` when a
    failure cannot be tied to a specific line). It subclasses the shared
    ``ForgeError`` base *and* :class:`ValueError` so callers can catch either.
    """

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        self.raw_message = message
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(f"{prefix}{message}")


# --------------------------------------------------------------------------- #
# Frontmatter metadata keys (everything on the manifest EXCEPT the typed lists  #
# and ``name`` — which is the ``## Goal`` section body).                        #
# --------------------------------------------------------------------------- #

#: Scalar / simple-list manifest fields carried by the YAML frontmatter.
_FRONTMATTER_KEYS: tuple[str, ...] = (
    "id",
    "status",
    "constitution_refs",
    "repos",
    "execution_mode",
    "skill_profile",
    "plan_ref",
    "tasks_ref",
    "validation_ref",
)

_H2 = "## "
_H3 = "### "
_ADR_SEP = " — "  # id — title (em dash, spaced)

# ``- **ID**: text`` (Requirements / Open Questions).
_BOLD_BULLET = re.compile(r"^- \*\*(?P<id>[^*]+)\*\*:\s?(?P<text>.*)$")
# ``- **ID** (refs): text`` (Acceptance Criteria; the parenthetical is optional).
_ACCEPT_BULLET = re.compile(r"^- \*\*(?P<id>[^*]+)\*\*(?: \((?P<refs>[^)]*)\))?:\s?(?P<text>.*)$")
# ``  - Resolution: text`` (indented sub-bullet under an open question).
_RESOLUTION = re.compile(r"^ {2}- Resolution:\s?(?P<text>.*)$")
# ``- Status|Context|Decision|Consequences: text`` (ADR fields).
_ADR_FIELD = re.compile(r"^- (?P<key>Status|Context|Decision|Consequences):\s?(?P<val>.*)$")

_ADR_FIELD_ATTR = {
    "Status": "status",
    "Context": "context",
    "Decision": "decision",
    "Consequences": "consequences",
}


# --------------------------------------------------------------------------- #
# Render                                                                       #
# --------------------------------------------------------------------------- #


def _frontmatter(manifest: SpecManifest) -> str:
    payload = manifest.model_dump(mode="json")
    ordered: dict[str, Any] = {key: payload[key] for key in _FRONTMATTER_KEYS}
    body = yaml.safe_dump(
        ordered,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return f"---\n{body}---"


def _acceptance_line(criterion: AcceptanceCriterion) -> str:
    inner: list[str] = []
    if criterion.req_refs:
        inner.append(", ".join(criterion.req_refs))
    if criterion.spec_ref:
        inner.append(f"spec={criterion.spec_ref}")
    paren = f" ({'; '.join(inner)})" if inner else ""
    return f"- **{criterion.id}**{paren}: {criterion.text}"


def _decision_block(adr: ADR) -> list[str]:
    lines = [f"{_H3}{adr.id}{_ADR_SEP}{adr.title}", "", f"- Status: {adr.status}"]
    if adr.context is not None:
        lines.append(f"- Context: {adr.context}")
    if adr.decision is not None:
        lines.append(f"- Decision: {adr.decision}")
    if adr.consequences is not None:
        lines.append(f"- Consequences: {adr.consequences}")
    return lines


def render_spec_md(manifest: SpecManifest) -> str:
    """Render ``manifest`` as ``spec.md`` markdown text (inverse of parse)."""
    parts: list[str] = [_frontmatter(manifest), "", f"{_H2}Goal", "", manifest.name]

    if manifest.requirements:
        parts += ["", f"{_H2}Requirements", ""]
        parts += [f"- **{r.id}**: {r.text}" for r in manifest.requirements]

    if manifest.acceptance_criteria:
        parts += ["", f"{_H2}Acceptance Criteria", ""]
        parts += [_acceptance_line(a) for a in manifest.acceptance_criteria]

    if manifest.constraints:
        parts += ["", f"{_H2}Constraints", ""]
        parts += [f"- {c}" for c in manifest.constraints]

    if manifest.open_questions:
        parts += ["", f"{_H2}Open Questions", ""]
        for q in manifest.open_questions:
            parts.append(f"- **{q.id}**: {q.text}")
            if q.resolution is not None:
                parts.append(f"  - Resolution: {q.resolution}")

    if manifest.decisions:
        parts += ["", f"{_H2}Decisions"]
        for adr in manifest.decisions:
            parts.append("")
            parts += _decision_block(adr)

    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Parse                                                                        #
# --------------------------------------------------------------------------- #


class _Section:
    """A ``##`` section: its header line number and (1-based line, text) body."""

    def __init__(self, title: str, header_line: int) -> None:
        self.title = title
        self.header_line = header_line
        self.lines: list[tuple[int, str]] = []


def _split_frontmatter(lines: list[str]) -> tuple[dict[str, Any], int]:
    """Return the parsed frontmatter mapping and the 0-based body start index."""
    idx = 0
    n = len(lines)
    while idx < n and lines[idx].strip() == "":
        idx += 1
    if idx >= n or lines[idx].strip() != "---":
        raise SpecParseError("spec.md must begin with a '---' YAML frontmatter block", line=idx + 1)
    open_line = idx + 1  # 1-based line number of the opening '---'
    idx += 1
    fm_body: list[str] = []
    while idx < n and lines[idx].strip() != "---":
        fm_body.append(lines[idx])
        idx += 1
    if idx >= n:
        raise SpecParseError("unterminated frontmatter: missing closing '---'", line=open_line)
    try:
        data = yaml.safe_load("\n".join(fm_body))
    except yaml.YAMLError as exc:  # pragma: no cover - message varies by input
        raise SpecParseError(f"invalid YAML frontmatter: {exc}", line=open_line + 1) from exc
    data = data or {}
    if not isinstance(data, dict):
        raise SpecParseError("frontmatter must be a YAML mapping", line=open_line + 1)
    return data, idx + 1  # skip the closing '---'


def _collect_sections(lines: list[str], start: int) -> list[_Section]:
    sections: list[_Section] = []
    current: _Section | None = None
    for offset in range(start, len(lines)):
        raw = lines[offset]
        line_no = offset + 1
        if raw.startswith(_H2):
            current = _Section(raw[len(_H2) :].strip(), line_no)
            sections.append(current)
            continue
        if current is None:
            if raw.strip() == "":
                continue
            raise SpecParseError("unexpected content before first '##' section", line=line_no)
        current.lines.append((line_no, raw))
    return sections


def _nonblank(section: _Section) -> list[tuple[int, str]]:
    return [(ln, text) for ln, text in section.lines if text.strip() != ""]


def _parse_goal(section: _Section) -> str:
    body = "\n".join(text for _, text in section.lines).strip()
    if not body:
        raise SpecParseError("## Goal section is empty", line=section.header_line)
    return body


def _parse_requirements(section: _Section) -> list[Requirement]:
    out: list[Requirement] = []
    for line_no, text in _nonblank(section):
        match = _BOLD_BULLET.match(text)
        if not match:
            raise SpecParseError("requirement must be '- **ID**: text'", line=line_no)
        out.append(Requirement(id=match["id"].strip(), text=match["text"].strip()))
    return out


def _parse_refs(refs: str | None) -> tuple[list[str], str | None]:
    if refs is None:
        return [], None
    req_refs: list[str] = []
    spec_ref: str | None = None
    for part in refs.split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        if chunk.startswith("spec="):
            spec_ref = chunk[len("spec=") :].strip() or None
        else:
            req_refs = [ref.strip() for ref in chunk.split(",") if ref.strip()]
    return req_refs, spec_ref


def _parse_acceptance(section: _Section) -> list[AcceptanceCriterion]:
    out: list[AcceptanceCriterion] = []
    for line_no, text in _nonblank(section):
        match = _ACCEPT_BULLET.match(text)
        if not match:
            raise SpecParseError(
                "acceptance criterion must be '- **ID** (refs): text'", line=line_no
            )
        req_refs, spec_ref = _parse_refs(match["refs"])
        out.append(
            AcceptanceCriterion(
                id=match["id"].strip(),
                text=match["text"].strip(),
                req_refs=req_refs,
                spec_ref=spec_ref,
            )
        )
    return out


def _parse_constraints(section: _Section) -> list[str]:
    out: list[str] = []
    for line_no, text in _nonblank(section):
        if not text.startswith("- "):
            raise SpecParseError("constraint must be a '- ' bullet", line=line_no)
        out.append(text[2:].strip())
    return out


def _parse_open_questions(section: _Section) -> list[OpenQuestion]:
    out: list[OpenQuestion] = []
    for line_no, text in _nonblank(section):
        resolution = _RESOLUTION.match(text)
        if resolution is not None:
            if not out:
                raise SpecParseError("resolution has no preceding open question", line=line_no)
            out[-1] = out[-1].model_copy(update={"resolution": resolution["text"].strip()})
            continue
        match = _BOLD_BULLET.match(text)
        if not match:
            raise SpecParseError("open question must be '- **ID**: text'", line=line_no)
        out.append(OpenQuestion(id=match["id"].strip(), text=match["text"].strip()))
    return out


def _parse_decisions(section: _Section) -> list[ADR]:
    out: list[ADR] = []
    fields: dict[str, str] = {}
    header: tuple[str, str] | None = None

    def flush() -> None:
        nonlocal fields, header
        if header is None:
            return
        adr_id, title = header
        out.append(ADR(id=adr_id, title=title, **fields))
        fields = {}
        header = None

    for line_no, text in _nonblank(section):
        if text.startswith(_H3):
            flush()
            body = text[len(_H3) :]
            if _ADR_SEP not in body:
                raise SpecParseError("decision heading must be '### ID — Title'", line=line_no)
            adr_id, title = body.split(_ADR_SEP, 1)
            header = (adr_id.strip(), title.strip())
            continue
        field = _ADR_FIELD.match(text)
        if field is None:
            raise SpecParseError(
                "decision field must be '- Status|Context|Decision|Consequences: text'",
                line=line_no,
            )
        if header is None:
            raise SpecParseError("decision field before any '### ID — Title'", line=line_no)
        fields[_ADR_FIELD_ATTR[field["key"]]] = field["val"].strip()
    flush()
    return out


def _parse_section(section: _Section, data: dict[str, Any]) -> None:
    """Parse one non-Goal ``##`` section into ``data`` (mutating it in place)."""
    if section.title == "Requirements":
        data["requirements"] = _parse_requirements(section)
    elif section.title == "Acceptance Criteria":
        data["acceptance_criteria"] = _parse_acceptance(section)
    elif section.title == "Constraints":
        data["constraints"] = _parse_constraints(section)
    elif section.title == "Open Questions":
        data["open_questions"] = _parse_open_questions(section)
    elif section.title == "Decisions":
        data["decisions"] = _parse_decisions(section)
    else:
        raise SpecParseError(f"unknown section '## {section.title}'", line=section.header_line)


def parse_spec_md(text: str) -> SpecManifest:
    """Parse ``spec.md`` markdown ``text`` into a :class:`SpecManifest`.

    The exact inverse of :func:`render_spec_md`. Raises :class:`SpecParseError`
    (line-anchored) on malformed input.
    """
    lines = text.splitlines()
    frontmatter, body_start = _split_frontmatter(lines)

    data: dict[str, Any] = {
        key: frontmatter[key] for key in _FRONTMATTER_KEYS if key in frontmatter
    }
    if "id" not in data:
        raise SpecParseError("frontmatter is missing required key 'id'", line=1)

    name: str | None = None
    for section in _collect_sections(lines, body_start):
        if section.title == "Goal":
            name = _parse_goal(section)
            continue
        _parse_section(section, data)

    if name is None:
        raise SpecParseError("spec.md is missing a '## Goal' section (the spec name)", line=1)
    data["name"] = name

    try:
        return SpecManifest.model_validate(data)
    except SpecParseError:
        raise
    except Exception as exc:  # pydantic ValidationError, enum coercion, etc.
        raise SpecParseError(f"frontmatter did not validate: {exc}", line=1) from exc


__all__ = ["SpecParseError", "parse_spec_md", "render_spec_md"]
