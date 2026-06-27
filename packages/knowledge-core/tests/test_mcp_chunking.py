"""Unit tests for the F20 mime-type-routed MCP chunker (AC13)."""

from __future__ import annotations

from forge_contracts.enums import ChunkType
from forge_knowledge.mcp_chunking import McpResourceChunker, McpResourceSnapshot, provenance_uri

SLUG = "confluence-engineering"


def _snap(
    content: str, mime: str | None, uri: str = "confluence://engineering/p"
) -> McpResourceSnapshot:
    return McpResourceSnapshot(
        uri=uri, content=content, connection_slug=SLUG, mime_type=mime, namespace="engineering"
    )


def test_markdown_is_split_on_headings() -> None:
    md = "# Title\n\nIntro paragraph.\n\n## Section\n\nMore text here."
    chunks = McpResourceChunker().chunk(_snap(md, "text/markdown"))
    assert len(chunks) >= 3
    assert all(c.chunk_type is ChunkType.MCP_RESOURCE for c in chunks)
    assert all(c.weight == 1.0 for c in chunks)
    # Provenance + per-resource path on every chunk.
    expected_path = provenance_uri(SLUG, "confluence://engineering/p")
    assert all(c.path == expected_path for c in chunks)
    assert {c.metadata["chunk_index"] for c in chunks} == set(range(len(chunks)))


def test_html_is_tag_stripped_then_split() -> None:
    html = (
        "<html><body><h1>Heading</h1><p>Hello <b>world</b></p><script>evil()</script></body></html>"
    )
    chunks = McpResourceChunker().chunk(_snap(html, "text/html"))
    joined = "\n".join(c.content for c in chunks)
    assert "Hello" in joined and "world" in joined
    assert "<" not in joined and ">" not in joined
    assert "evil()" not in joined  # script bodies are dropped


def test_json_is_structured_split() -> None:
    payload = '{"alpha": {"k": 1}, "beta": [1, 2, 3]}'
    chunks = McpResourceChunker().chunk(_snap(payload, "application/json"))
    contents = [c.content for c in chunks]
    assert any(c.startswith("alpha:") for c in contents)
    assert any(c.startswith("beta:") for c in contents)
    assert all(c.chunk_type is ChunkType.MCP_RESOURCE for c in chunks)


def test_plain_text_and_unknown_mime_use_text_splitter() -> None:
    chunks = McpResourceChunker().chunk(_snap("line one\n\nline two", "text/plain"))
    assert chunks and all(c.chunk_type is ChunkType.MCP_RESOURCE for c in chunks)
    # None mime is treated as text.
    none_chunks = McpResourceChunker().chunk(_snap("some content", None))
    assert none_chunks


def test_binary_resource_is_skipped() -> None:
    assert McpResourceChunker().chunk(_snap("\x00\x01PNGDATA", "image/png")) == []
    assert McpResourceChunker().chunk(_snap("blob", "application/octet-stream")) == []


def test_empty_content_yields_no_chunks() -> None:
    assert McpResourceChunker().chunk(_snap("   ", "text/markdown")) == []
