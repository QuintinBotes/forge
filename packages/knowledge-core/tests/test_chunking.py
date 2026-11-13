# ruff: noqa: RUF001, RUF002, RUF003 - the breadcrumb separator is "›" (spec J §2.4.1)
"""Tests for ``forge_knowledge.chunking`` (plan Task 1.1, RAG spine).

TDD coverage for the chunking layer of the Knowledge/RAG pipeline:

- ``chunk_code`` produces one chunk per top-level ``def``/``class`` with line
  spans whose ``content`` exactly reconstructs from the source (incl. decorators),
  a module-preamble chunk for imports/constants, nested defs are NOT split out,
  and malformed/empty input degrades gracefully (ast fallback — never raises);
- ``chunk_markdown`` chunks by section (a heading and its body up to the next
  heading of the same or a higher level), packs over-long sections at paragraph
  boundaries, keeps fenced code blocks and lists intact, starts every chunk with
  a ``path › headings (status: …)`` breadcrumb line, and splits very long
  paragraphs by ``max_chars``;
- chunk-type weights match the frozen ``CHUNK_TYPE_WEIGHTS`` table (README 1.3,
  policy/AGENTS.md 1.5, spec 1.4, summary 1.2, default 1.0).

All hermetic: no external services, no network.
"""

from __future__ import annotations

import pytest

from forge_contracts.constants import CHUNK_TYPE_WEIGHTS
from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType
from forge_knowledge.chunking import (
    chunk_code,
    chunk_file,
    chunk_markdown,
    classify_path,
    weight_for,
)

SAMPLE_PY = '''"""Module docstring."""
import os
import sys

CONST = 42


def alpha(x):
    return x + 1


@deco
def beta(y):
    """Beta."""
    return y * 2


class Gamma:
    """A class."""

    def method(self):
        return None
'''

SAMPLE_MD = """# Title

Intro paragraph one.
Still paragraph one.

## Section A

Para under A.

```python
x = 1

y = 2
```

## Section B

Para under B.
"""


def _by_symbol(chunks: list[Chunk]) -> dict[str, Chunk]:
    # Real definition symbols only — excludes the "<module>" preamble chunk.
    return {c.symbol: c for c in chunks if c.symbol and c.symbol != "<module>"}


# --------------------------------------------------------------------------- #
# chunk_code                                                                   #
# --------------------------------------------------------------------------- #


def test_chunk_code_one_chunk_per_top_level_def_and_class() -> None:
    chunks = chunk_code("app/sample.py", SAMPLE_PY)
    by_symbol = _by_symbol(chunks)
    # one chunk per top-level def/class (method is nested, not separate)
    assert set(by_symbol) == {"alpha", "beta", "Gamma"}
    assert "method" not in by_symbol


def test_chunk_code_line_spans_reconstruct_content_exactly() -> None:
    src_lines = SAMPLE_PY.splitlines()
    chunks = chunk_code("app/sample.py", SAMPLE_PY)
    for c in chunks:
        assert c.start_line is not None
        assert c.end_line is not None
        reconstructed = "\n".join(src_lines[c.start_line - 1 : c.end_line])
        assert c.content == reconstructed


def test_chunk_code_exact_spans() -> None:
    by_symbol = _by_symbol(chunk_code("app/sample.py", SAMPLE_PY))
    assert (by_symbol["alpha"].start_line, by_symbol["alpha"].end_line) == (8, 9)
    # decorator line is included in the span
    assert (by_symbol["beta"].start_line, by_symbol["beta"].end_line) == (12, 15)
    assert by_symbol["beta"].content.splitlines()[0] == "@deco"
    assert (by_symbol["Gamma"].start_line, by_symbol["Gamma"].end_line) == (18, 22)


def test_chunk_code_metadata() -> None:
    by_symbol = _by_symbol(chunk_code("app/sample.py", SAMPLE_PY))
    alpha = by_symbol["alpha"]
    assert alpha.chunk_type is ChunkType.CODE
    assert alpha.language == "python"
    assert alpha.path == "app/sample.py"
    assert alpha.weight == 1.0
    assert alpha.content_hash  # populated for incremental sync dedup


def test_chunk_code_module_preamble_captures_imports() -> None:
    chunks = chunk_code("app/sample.py", SAMPLE_PY)
    module_chunks = [c for c in chunks if c.symbol == "<module>"]
    assert len(module_chunks) == 1
    preamble = module_chunks[0]
    assert "import os" in preamble.content
    assert "CONST = 42" in preamble.content
    assert preamble.start_line == 1


def test_chunk_code_returns_contract_chunks() -> None:
    chunks = chunk_code("app/sample.py", SAMPLE_PY)
    assert chunks
    for c in chunks:
        assert isinstance(c, Chunk)
        # round-trips through the frozen DTO
        assert Chunk.model_validate(c.model_dump()) == c


def test_chunk_code_syntax_error_falls_back_to_whole_file() -> None:
    bad = "def broken(:\n    pass\n"
    chunks = chunk_code("app/broken.py", bad)
    assert len(chunks) == 1
    assert chunks[0].content == bad
    assert chunks[0].chunk_type is ChunkType.CODE


def test_chunk_code_empty_source_yields_no_chunks() -> None:
    assert chunk_code("app/empty.py", "") == []
    assert chunk_code("app/empty.py", "   \n  \n") == []


def test_chunk_code_async_def_is_chunked() -> None:
    src = "async def handler(req):\n    return req\n"
    by_symbol = _by_symbol(chunk_code("app/a.py", src))
    assert "handler" in by_symbol


# --------------------------------------------------------------------------- #
# chunk_markdown                                                               #
# --------------------------------------------------------------------------- #


def _body(chunk: Chunk) -> str:
    """A markdown chunk's content after its breadcrumb line."""
    return chunk.content.split("\n", 1)[1]


def test_chunk_markdown_small_section_is_one_chunk() -> None:
    # Spec J §2.4.1 D-a: a heading and everything under it is one chunk when
    # it fits, behind a "{path} › {headings}" breadcrumb line.
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD)
    assert len(chunks) == 1
    assert chunks[0].content == "docs/guide.md › Title\n" + SAMPLE_MD.rstrip("\n")


def test_chunk_markdown_splits_sections_at_paragraph_boundaries() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD, max_chars=80)
    assert [_body(c) for c in chunks] == [
        "# Title\n\nIntro paragraph one.\nStill paragraph one.",
        "## Section A\n\nPara under A.",
        "```python\nx = 1\n\ny = 2\n```",
        "## Section B\n\nPara under B.",
    ]
    assert all(len(c.content) <= 80 for c in chunks)


def test_chunk_markdown_keeps_fenced_code_block_intact() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD, max_chars=80)
    fenced = [c for c in chunks if _body(c).startswith("```python")]
    assert len(fenced) == 1
    # the blank line inside the fence must NOT have split the block
    assert "x = 1" in fenced[0].content
    assert "y = 2" in fenced[0].content
    assert fenced[0].content.strip().endswith("```")


def test_chunk_markdown_heading_breadcrumb_attribution() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD, max_chars=80)
    para_a = next(c for c in chunks if "Para under A." in c.content)
    assert para_a.metadata["heading_path"] == "Title > Section A"
    assert para_a.content.startswith("docs/guide.md › Title › Section A\n")
    para_b = next(c for c in chunks if "Para under B." in c.content)
    assert para_b.metadata["heading_path"] == "Title > Section B"
    assert para_b.content.startswith("docs/guide.md › Title › Section B\n")


def test_chunk_markdown_line_spans_reconstruct_content() -> None:
    src_lines = SAMPLE_MD.splitlines()
    for max_chars in (80, 1200):
        for c in chunk_markdown("docs/guide.md", SAMPLE_MD, max_chars=max_chars):
            assert c.start_line is not None
            assert c.end_line is not None
            reconstructed = "\n".join(src_lines[c.start_line - 1 : c.end_line])
            assert _body(c) == reconstructed


def test_chunk_markdown_empty_yields_no_chunks() -> None:
    assert chunk_markdown("docs/empty.md", "") == []
    assert chunk_markdown("docs/empty.md", "\n\n   \n") == []


def test_chunk_markdown_splits_long_paragraph_by_max_chars() -> None:
    long_para = "\n".join(f"line number {i} with some filler text" for i in range(200))
    chunks = chunk_markdown("docs/long.md", long_para, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c.content) <= 200 for c in chunks)
    # every source line survives the split
    joined = "\n".join(c.content for c in chunks)
    for i in range(200):
        assert f"line number {i} with some filler text" in joined


REQ_MD = """# Payments spec

Status: Draft

Background paragraph that explains why the payments service exists at all.

## Requirements

The service must meet every requirement below.

- R1: validate every oauth token before charging
- R2: refuse a charge whose currency is unknown

- R3: write one ledger entry per charge
- R4: never retry a declined card

A closing paragraph about requirements that is long enough to force a split here.

## Amendment 2026-10-01

First change.

## Amendment (2026-11-08)

Second change.
"""


def test_chunk_markdown_keeps_a_requirement_list_whole_with_breadcrumb_and_status() -> None:
    # Spec J §2.4.1 D-a: lore's causes F (part of a list) and S (status lost).
    chunks = chunk_markdown("docs/payments.md", REQ_MD, max_chars=300)
    with_list = [c for c in chunks if "R1:" in c.content or "R4:" in c.content]
    assert len(with_list) == 1, [c.content for c in chunks]
    assert all(f"R{i}:" in with_list[0].content for i in range(1, 5))
    header = "docs/payments.md › Payments spec › Requirements (status: Draft, amended 2026-11-08)"
    assert with_list[0].content.startswith(header + "\n")
    assert len(chunks) > 3  # the section really was split
    for chunk in chunks:
        assert chunk.content.startswith("docs/payments.md")
        assert "(status: Draft, amended 2026-11-08)\n" in chunk.content


def test_chunk_markdown_splits_a_list_only_when_it_alone_is_too_long() -> None:
    items = "\n".join(f"- requirement number {i} must hold" for i in range(20))
    chunks = chunk_markdown("docs/long.md", f"# Reqs\n\n{items}\n", max_chars=200)
    assert len(chunks) > 2
    joined = "\n".join(_body(c) for c in chunks)
    for i in range(20):
        assert f"requirement number {i} must hold" in joined


def test_chunk_markdown_status_from_front_matter_and_none_omits_parenthesis() -> None:
    front = chunk_markdown("docs/adr.md", "---\nstatus: accepted\n---\n# ADR\n\nBody.\n")
    assert all("(status: accepted)" in c.content.splitlines()[0] for c in front)
    bold = chunk_markdown("docs/adr.md", "# ADR\n\n**Status:** Superseded\n\nBody.\n")
    assert bold[0].content.startswith("docs/adr.md › ADR (status: Superseded)\n")
    plain = chunk_markdown("docs/guide.md", "# Guide\n\nNo state here.\n")
    assert plain[0].content.splitlines()[0] == "docs/guide.md › Guide"


def test_chunk_markdown_keeps_a_list_whole_when_only_the_header_pushes_it_over() -> None:
    # Longer than max_chars minus the breadcrumb line, shorter than max_chars.
    items = "\n".join(f"- REQ-{i:02d}: the system shall do thing {i}" for i in range(5))
    header = "docs/specs/requirements.md › Spec › Section › Requirements"
    assert 200 - len(header) < len(items) < 200
    src = f"# Spec\n\n## Section\n\n### Requirements\n\n{items}\n"

    chunks = chunk_markdown("docs/specs/requirements.md", src, max_chars=200)

    [chunk] = [c for c in chunks if "REQ-" in c.content]
    assert all(f"REQ-{i:02d}" in chunk.content for i in range(5))
    assert chunk.content.startswith(header + "\n")


def test_chunk_markdown_never_emits_a_heading_on_its_own() -> None:
    para = "word " * 30
    src = f"# Title\n\n## Part\n\n{para}\n\n## Next\n\n{para}\n"
    chunks = chunk_markdown("docs/x.md", src, max_chars=120)
    bodies = [_body(c) for c in chunks]
    assert not [b for b in bodies if all(_is_heading(line) for line in b.splitlines() if line)]
    assert bodies[0].startswith("# Title\n\n## Part\n\nword")
    assert any(b.startswith("## Next\n\nword") for b in bodies)


def test_chunk_markdown_an_empty_section_opens_the_next_one() -> None:
    src = "# Doc\n\nIntro.\n\n## Notes\n\n## Decision\n\nUse Postgres.\n\n## Empty end\n"
    chunks = chunk_markdown("docs/x.md", src, max_chars=60)
    bodies = [_body(c) for c in chunks]
    assert "## Notes\n\n## Decision\n\nUse Postgres." in bodies
    assert not any(b.startswith("## Notes") and "Decision" not in b for b in bodies)
    # Nothing follows the last heading, so it stays (once).
    assert sum("## Empty end" in b for b in bodies) == 1
    src_lines = src.splitlines()
    for c in chunks:
        assert _body(c) == "\n".join(src_lines[c.start_line - 1 : c.end_line])


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _status(src: str) -> str:
    """The breadcrumb line of the document's first chunk."""
    return chunk_markdown("docs/adr.md", src)[0].content.splitlines()[0]


def test_status_from_a_status_line_before_the_first_h2() -> None:
    assert _status("# ADR\n\nStatus: Accepted\n\n## Context\n\nx\n").endswith("(status: Accepted)")
    # After the first H2 a Status: line is body text.
    assert _status("# ADR\n\n## Context\n\nStatus: Accepted\n") == "docs/adr.md › ADR"


def test_status_from_the_first_paragraph_under_a_status_heading() -> None:
    src = "# ADR-0100\n\n## Status\n\nSuperseded by\nADR-0101\n\n## Example\n\n> Status: x\n"
    assert _status(src) == "docs/adr.md › ADR-0100 (status: Superseded by ADR-0101)"


def test_status_front_matter_wins_and_needs_a_top_level_key() -> None:
    front = "---\ntitle: X\nstatus: proposed\n---\n\n# X\n\nStatus: Approved\n"
    assert _status(front).endswith("(status: proposed)")
    nested = "---\ntitle: X\nreview:\n  status: pending-legal\n---\n\n# X\n\nBody.\n"
    assert "status" not in _status(nested)


@pytest.mark.parametrize(
    "line",
    ["- status: done", "* Status: done", "> Status: Accepted", "  Status: indented"],
)
def test_status_ignores_list_items_and_quotes(line: str) -> None:
    assert _status(f"# ADR\n\n{line}\n\nBody.\n") == "docs/adr.md › ADR"


def test_status_ignores_a_status_line_inside_a_fence() -> None:
    src = "# ADR\n\n```yaml\nStatus: Accepted\n```\n\nBody.\n"
    assert _status(src) == "docs/adr.md › ADR"
    fenced_heading = "# ADR\n\n```\n## Status\n\nAccepted\n```\n"
    assert _status(fenced_heading) == "docs/adr.md › ADR"


def test_status_is_capped_in_length() -> None:
    header = _status(f"# ADR\n\nStatus: {'very long status ' * 20}\n")
    status = header.split("(status: ", 1)[1].removesuffix(")")
    assert len(status) == 120
    assert status.startswith("very long status very")


# --------------------------------------------------------------------------- #
# weights + classification                                                     #
# --------------------------------------------------------------------------- #


def test_weight_for_matches_frozen_table() -> None:
    for chunk_type, weight in CHUNK_TYPE_WEIGHTS.items():
        assert weight_for(chunk_type) == weight


def test_classify_path_rules() -> None:
    assert classify_path("README.md") is ChunkType.README
    assert classify_path("docs/README.rst") is ChunkType.README
    assert classify_path("AGENTS.md") is ChunkType.POLICY
    assert classify_path(".forge/policy.yaml") is ChunkType.POLICY
    assert classify_path("specs/SPEC-1/spec.md") is ChunkType.SPEC
    assert classify_path("docs/plan.md") is ChunkType.SPEC
    assert classify_path("docs/guide.md") is ChunkType.MARKDOWN
    assert classify_path("app/main.py") is ChunkType.CODE
    assert classify_path(None) is ChunkType.MARKDOWN


def test_weights_applied_to_markdown_chunks() -> None:
    body = "# Heading\n\nbody paragraph\n"
    readme = chunk_markdown("README.md", body)
    assert all(c.weight == 1.3 for c in readme)
    assert all(c.chunk_type is ChunkType.README for c in readme)

    agents = chunk_markdown("AGENTS.md", body)
    assert all(c.weight == 1.5 for c in agents)
    assert all(c.chunk_type is ChunkType.POLICY for c in agents)

    spec = chunk_markdown("specs/SPEC-1/spec.md", body)
    assert all(c.weight == 1.4 for c in spec)
    assert all(c.chunk_type is ChunkType.SPEC for c in spec)

    plain = chunk_markdown("docs/guide.md", body)
    assert all(c.weight == 1.0 for c in plain)
    assert all(c.chunk_type is ChunkType.MARKDOWN for c in plain)


def test_weights_applied_to_code_chunks() -> None:
    chunks = chunk_code("app/main.py", "def f():\n    return 1\n")
    assert all(c.weight == 1.0 for c in chunks)
    assert all(c.chunk_type is ChunkType.CODE for c in chunks)


# --------------------------------------------------------------------------- #
# dispatcher                                                                   #
# --------------------------------------------------------------------------- #


def test_chunk_file_dispatches_python_to_ast() -> None:
    chunks = chunk_file("app/main.py", SAMPLE_PY)
    assert {"alpha", "beta", "Gamma"} <= {c.symbol for c in chunks if c.symbol}


def test_chunk_file_dispatches_markdown() -> None:
    chunks = chunk_file("README.md", "# Title\n\nbody\n")
    assert [c.content for c in chunks] == ["README.md › Title\n# Title\n\nbody"]
    assert all(c.chunk_type is ChunkType.README for c in chunks)


def test_chunk_file_content_hash_is_stable() -> None:
    a = chunk_code("app/main.py", SAMPLE_PY)
    b = chunk_code("app/main.py", SAMPLE_PY)
    assert [c.content_hash for c in a] == [c.content_hash for c in b]
    assert all(c.content_hash for c in a)


def test_status_needs_front_matter_at_the_top() -> None:
    [chunk, *_] = chunk_markdown("d.md", "# T\n\nstatus: x\n\n---\n\nbody\n")
    assert "(status:" not in chunk.content.splitlines()[0]


def test_status_under_a_status_heading_is_never_a_list_quote_or_comment() -> None:
    for block in ("- Accepted", "> Accepted", "<!-- Accepted -->"):
        [chunk, *_] = chunk_markdown("d.md", f"# S\n\n## Status\n\n{block}\n")
        assert "(status:" not in chunk.content.splitlines()[0], block
    [chunk, *_] = chunk_markdown("d.md", "# S\n\n<!--\nStatus: hidden\n-->\n\nx\n")
    assert "(status:" not in chunk.content.splitlines()[0]


def test_nested_empty_sections_never_make_a_heading_only_chunk() -> None:
    p1, p2 = "alpha " * 12, "beta " * 12
    for md, size in (
        (f"# T\n\n{p1}\n\n## A\n\n### A1\n\n## B\n\n{p2}\n", 100),
        (f"# T\n\n## A\n\n### A1\n\n## B\n\n{p2}\n", 60),
    ):
        for chunk in chunk_markdown("d.md", md, max_chars=size):
            body = chunk.content.split("\n", 1)[1]
            assert any(
                line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()
            ), chunk.content
