"""Tests for ``forge_knowledge.chunking`` (plan Task 1.1, RAG spine).

TDD coverage for the chunking layer of the Knowledge/RAG pipeline:

- ``chunk_code`` produces one chunk per top-level ``def``/``class`` with line
  spans whose ``content`` exactly reconstructs from the source (incl. decorators),
  a module-preamble chunk for imports/constants, nested defs are NOT split out,
  and malformed/empty input degrades gracefully (ast fallback — never raises);
- ``chunk_markdown`` splits on headings AND paragraphs, keeps fenced code blocks
  intact, carries a heading breadcrumb for source attribution, and splits very
  long paragraphs by ``max_chars``;
- chunk-type weights match the frozen ``CHUNK_TYPE_WEIGHTS`` table (README 1.3,
  policy/AGENTS.md 1.5, spec 1.4, summary 1.2, default 1.0).

All hermetic: no external services, no network.
"""

from __future__ import annotations

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


def test_chunk_markdown_splits_headings_and_paragraphs() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD)
    contents = [c.content for c in chunks]
    assert "# Title" in contents
    assert "## Section A" in contents
    assert "## Section B" in contents
    assert "Intro paragraph one.\nStill paragraph one." in contents
    assert "Para under A." in contents
    assert "Para under B." in contents


def test_chunk_markdown_keeps_fenced_code_block_intact() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD)
    fenced = [c for c in chunks if c.content.startswith("```python")]
    assert len(fenced) == 1
    # the blank line inside the fence must NOT have split the block
    assert "x = 1" in fenced[0].content
    assert "y = 2" in fenced[0].content
    assert fenced[0].content.strip().endswith("```")


def test_chunk_markdown_heading_breadcrumb_attribution() -> None:
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD)
    para_a = next(c for c in chunks if c.content == "Para under A.")
    assert para_a.metadata["heading_path"] == "Title > Section A"
    para_b = next(c for c in chunks if c.content == "Para under B.")
    assert para_b.metadata["heading_path"] == "Title > Section B"


def test_chunk_markdown_line_spans_reconstruct_content() -> None:
    src_lines = SAMPLE_MD.splitlines()
    chunks = chunk_markdown("docs/guide.md", SAMPLE_MD)
    for c in chunks:
        assert c.start_line is not None
        assert c.end_line is not None
        reconstructed = "\n".join(src_lines[c.start_line - 1 : c.end_line])
        assert c.content == reconstructed


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
    assert any(c.content == "# Title" for c in chunks)
    assert all(c.chunk_type is ChunkType.README for c in chunks)


def test_chunk_file_content_hash_is_stable() -> None:
    a = chunk_code("app/main.py", SAMPLE_PY)
    b = chunk_code("app/main.py", SAMPLE_PY)
    assert [c.content_hash for c in a] == [c.content_hash for c in b]
    assert all(c.content_hash for c in a)
