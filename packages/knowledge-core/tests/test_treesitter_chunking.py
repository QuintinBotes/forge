"""Tests for the tree-sitter multi-language chunking backend (Task H2).

The tree-sitter backend (:mod:`forge_knowledge.treesitter_chunking`) is the
primary code chunker behind the existing :func:`forge_knowledge.chunk_code` /
:func:`forge_knowledge.chunk_file` interface; the stdlib ``ast`` chunker remains
the Python fallback. These tests prove:

* multi-language chunking for JavaScript, TypeScript, and Go (>=2 languages),
  with one chunk per top-level definition and a ``<module>`` preamble for the
  rest (mirroring the ``ast`` chunker's shape);
* the span/content contract holds — every chunk reconstructs from its line span;
* :func:`chunk_file` routes recognised code extensions through tree-sitter;
* graceful fallback: with tree-sitter unavailable, Python degrades to the
  stdlib ``ast`` path and other code degrades to whole-file / text chunking;
* an error parse degrades to a single whole-file chunk (never raises).

All hermetic: pure in-process parsing, no network, no external services.
"""

from __future__ import annotations

import forge_knowledge.chunking as chunking
from forge_contracts.enums import ChunkType
from forge_knowledge import (
    TREE_SITTER_LANGUAGES,
    chunk_code,
    chunk_file,
    treesitter_available,
)
from forge_knowledge.treesitter_chunking import (
    chunk_with_treesitter,
    language_for_path,
)

JS_SRC = """import x from "y";

export function greet(name) {
  return "hi " + name;
}

class Service {
  run() {
    return 1;
  }
}

const arrow = (a) => a * 2;
"""

TS_SRC = """interface Props {
  id: number;
}

export function greet(name: string): string {
  return `hi ${name}`;
}

export class Service {
  run(): void {}
}
"""

GO_SRC = (
    "package main\n"
    "\n"
    'import "fmt"\n'
    "\n"
    "func Add(a int, b int) int {\n"
    "\treturn a + b\n"
    "}\n"
    "\n"
    "type Server struct {\n"
    "\tAddr string\n"
    "}\n"
    "\n"
    "func (s *Server) Start() error {\n"
    "\treturn nil\n"
    "}\n"
)


def _real_symbols(chunks: list) -> set[str]:
    return {c.symbol for c in chunks if c.symbol and c.symbol != "<module>"}


def _assert_reconstructs(src: str, chunks: list) -> None:
    src_lines = src.splitlines()
    for c in chunks:
        assert c.start_line is not None and c.end_line is not None
        assert c.content == "\n".join(src_lines[c.start_line - 1 : c.end_line])


# --------------------------------------------------------------------------- #
# availability / registry                                                      #
# --------------------------------------------------------------------------- #


def test_tree_sitter_languages_registered() -> None:
    assert {"python", "javascript", "typescript", "tsx", "go"} <= TREE_SITTER_LANGUAGES


def test_grammars_available_in_this_environment() -> None:
    # Task H2 ships these grammars as hard deps — they must be importable.
    for language in ("python", "javascript", "typescript", "go"):
        assert treesitter_available(language), language


def test_language_for_path_maps_extensions() -> None:
    assert language_for_path("pkg/app.py") == "python"
    assert language_for_path("src/index.js") == "javascript"
    assert language_for_path("src/index.jsx") == "javascript"
    assert language_for_path("src/index.ts") == "typescript"
    assert language_for_path("src/App.tsx") == "tsx"
    assert language_for_path("cmd/main.go") == "go"
    assert language_for_path("docs/guide.md") is None
    assert language_for_path("Makefile") is None
    assert language_for_path(None) is None


# --------------------------------------------------------------------------- #
# JavaScript                                                                   #
# --------------------------------------------------------------------------- #


def test_javascript_chunks_functions_and_classes() -> None:
    chunks = chunk_code("src/app.js", JS_SRC, language="javascript")
    assert _real_symbols(chunks) == {"greet", "Service"}
    assert all(c.chunk_type is ChunkType.CODE for c in chunks)
    assert all(c.language == "javascript" for c in chunks)


def test_javascript_export_span_includes_export_keyword() -> None:
    chunks = chunk_code("src/app.js", JS_SRC, language="javascript")
    greet = next(c for c in chunks if c.symbol == "greet")
    assert greet.content.splitlines()[0].startswith("export function greet")


def test_javascript_module_preamble_groups_non_defs() -> None:
    chunks = chunk_code("src/app.js", JS_SRC, language="javascript")
    module_text = "\n".join(c.content for c in chunks if c.symbol == "<module>")
    assert 'import x from "y";' in module_text
    assert "const arrow" in module_text


def test_javascript_spans_reconstruct() -> None:
    _assert_reconstructs(JS_SRC, chunk_code("src/app.js", JS_SRC, language="javascript"))


# --------------------------------------------------------------------------- #
# TypeScript                                                                   #
# --------------------------------------------------------------------------- #


def test_typescript_chunks_interface_function_class() -> None:
    chunks = chunk_code("src/app.ts", TS_SRC, language="typescript")
    assert _real_symbols(chunks) == {"Props", "greet", "Service"}
    assert all(c.language == "typescript" for c in chunks)


def test_typescript_spans_reconstruct() -> None:
    _assert_reconstructs(TS_SRC, chunk_code("src/app.ts", TS_SRC, language="typescript"))


# --------------------------------------------------------------------------- #
# Go                                                                           #
# --------------------------------------------------------------------------- #


def test_go_chunks_func_method_and_type() -> None:
    chunks = chunk_code("cmd/main.go", GO_SRC, language="go")
    assert _real_symbols(chunks) == {"Add", "Server", "Start"}
    assert all(c.language == "go" for c in chunks)


def test_go_module_preamble_captures_package_and_imports() -> None:
    chunks = chunk_code("cmd/main.go", GO_SRC, language="go")
    module_text = "\n".join(c.content for c in chunks if c.symbol == "<module>")
    assert "package main" in module_text
    assert 'import "fmt"' in module_text


def test_go_spans_reconstruct() -> None:
    _assert_reconstructs(GO_SRC, chunk_code("cmd/main.go", GO_SRC, language="go"))


# --------------------------------------------------------------------------- #
# Python via tree-sitter (primary backend)                                     #
# --------------------------------------------------------------------------- #


def test_python_uses_treesitter_backend() -> None:
    # When tree-sitter is available, chunk_with_treesitter returns chunks (not
    # None), i.e. it — not the ast fallback — is the active Python backend.
    src = "import os\n\n\ndef alpha(x):\n    return x + 1\n"
    chunks = chunk_with_treesitter("app/a.py", src, "python")
    assert chunks is not None
    assert "alpha" in _real_symbols(chunks)


# --------------------------------------------------------------------------- #
# chunk_file dispatch                                                          #
# --------------------------------------------------------------------------- #


def test_chunk_file_routes_code_extensions_to_treesitter() -> None:
    for path, src, lang in (
        ("src/app.js", JS_SRC, "javascript"),
        ("src/app.ts", TS_SRC, "typescript"),
        ("cmd/main.go", GO_SRC, "go"),
    ):
        chunks = chunk_file(path, src)
        assert chunks, path
        assert all(c.chunk_type is ChunkType.CODE for c in chunks), path
        assert all(c.language == lang for c in chunks), path
        assert _real_symbols(chunks), path


# --------------------------------------------------------------------------- #
# error handling                                                               #
# --------------------------------------------------------------------------- #


def test_parse_error_returns_none_for_fallback() -> None:
    assert chunk_with_treesitter("src/bad.js", "function (\n", "javascript") is None


def test_error_source_degrades_to_whole_file_chunk() -> None:
    bad = "function (\n"
    chunks = chunk_code("src/bad.js", bad, language="javascript")
    assert len(chunks) == 1
    assert chunks[0].content == bad
    assert chunks[0].chunk_type is ChunkType.CODE


def test_empty_source_yields_no_chunks() -> None:
    assert chunk_with_treesitter("src/app.js", "", "javascript") == []
    assert chunk_code("src/app.js", "   \n\n", language="javascript") == []


def test_unknown_language_returns_none() -> None:
    assert chunk_with_treesitter("a.rb", "puts 1\n", "ruby") is None


# --------------------------------------------------------------------------- #
# graceful fallback when tree-sitter is unavailable                            #
# --------------------------------------------------------------------------- #


def test_python_falls_back_to_ast_without_treesitter(monkeypatch) -> None:
    # Simulate tree-sitter being absent: chunk_code must still chunk Python via
    # the stdlib ``ast`` path (the documented fallback).
    monkeypatch.setattr(chunking, "chunk_with_treesitter", lambda *a, **k: None)
    src = "import os\n\n\ndef alpha(x):\n    return x + 1\n\n\nclass Gamma:\n    pass\n"
    chunks = chunk_code("app/a.py", src, language="python")
    assert {"alpha", "Gamma"} <= _real_symbols(chunks)


def test_non_python_code_falls_back_to_whole_file_without_treesitter(
    monkeypatch,
) -> None:
    monkeypatch.setattr(chunking, "chunk_with_treesitter", lambda *a, **k: None)
    chunks = chunk_code("cmd/main.go", GO_SRC, language="go")
    assert len(chunks) == 1
    assert chunks[0].content == GO_SRC
    assert chunks[0].chunk_type is ChunkType.CODE


def test_chunk_file_falls_back_to_text_without_treesitter(monkeypatch) -> None:
    # With no grammar available, chunk_file routes code through the text chunker
    # (prior behaviour) instead of producing a useless whole-file chunk.
    monkeypatch.setattr(chunking, "treesitter_available", lambda language: False)
    chunks = chunk_file("src/app.js", JS_SRC)
    assert chunks
    # Text/paragraph chunker labels language "markdown"; classify_path keeps the
    # CODE type for a .js extension.
    assert all(c.language == "markdown" for c in chunks)
    assert all(c.chunk_type is ChunkType.CODE for c in chunks)
