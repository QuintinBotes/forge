"""Tree-sitter multi-language code chunking backend (plan Task 1.1, V1 hardening).

This is the multi-language counterpart to the stdlib ``ast`` chunker in
:mod:`forge_knowledge.chunking`. It chunks source into one chunk per top-level
definition (function / class / method / type), grouping the remaining top-level
statements (imports, package clause, constants) into contiguous ``<module>``
preamble chunks — exactly the shape the ``ast`` path produces for Python, but
generalised across languages via `tree-sitter <https://tree-sitter.github.io>`_
grammars.

Supported languages (extension → grammar): Python, JavaScript/JSX, TypeScript,
TSX, and Go. Each language declares the node types that count as a top-level
*definition* and the *wrapper* node types (``decorated_definition`` in Python,
``export_statement`` in JS/TS) that must be unwrapped to find the real
definition and its name.

Graceful degradation is a first-class concern: if ``tree_sitter`` or a grammar
package is not installed, :func:`chunk_with_treesitter` returns ``None`` so the
caller can fall back to the stdlib ``ast`` path (Python) or a whole-file chunk.
The package therefore imports and runs even with no tree-sitter installed.

Span/content contract (shared with the ``ast`` chunker): a chunk's
``content`` is reconstructable as ``"\\n".join(src_lines[start_line-1:end_line])``
— content is sliced from the source lines, not the tree-sitter byte range, so
line spans and content never disagree.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING

from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType

if TYPE_CHECKING:
    from tree_sitter import Node

__all__ = [
    "TREE_SITTER_LANGUAGES",
    "chunk_with_treesitter",
    "language_for_path",
    "treesitter_available",
]


# --------------------------------------------------------------------------- #
# Language registry                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _LanguageSpec:
    """How to load and walk one tree-sitter grammar."""

    #: Importable module providing the compiled grammar (e.g. ``tree_sitter_go``).
    module: str
    #: Attribute on that module returning the grammar pointer/capsule.
    loader: str
    #: Node types that are a top-level definition → their own chunk.
    def_types: frozenset[str]
    #: Wrapper node types whose unwrapped child may be a definition.
    wrapper_types: frozenset[str] = field(default_factory=frozenset)


# Node-type vocabularies per grammar (tree-sitter grammar node names).
_PY_DEFS = frozenset({"function_definition", "class_definition"})
_JS_DEFS = frozenset(
    {"function_declaration", "generator_function_declaration", "class_declaration"}
)
_TS_DEFS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_signature",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
        "internal_module",
        "module",
    }
)
_GO_DEFS = frozenset({"function_declaration", "method_declaration", "type_declaration"})

#: Registry of every language this backend understands.
_LANGUAGES: dict[str, _LanguageSpec] = {
    "python": _LanguageSpec(
        "tree_sitter_python", "language", _PY_DEFS, frozenset({"decorated_definition"})
    ),
    "javascript": _LanguageSpec(
        "tree_sitter_javascript", "language", _JS_DEFS, frozenset({"export_statement"})
    ),
    "typescript": _LanguageSpec(
        "tree_sitter_typescript",
        "language_typescript",
        _TS_DEFS,
        frozenset({"export_statement"}),
    ),
    "tsx": _LanguageSpec(
        "tree_sitter_typescript", "language_tsx", _TS_DEFS, frozenset({"export_statement"})
    ),
    "go": _LanguageSpec("tree_sitter_go", "language", _GO_DEFS),
}

#: Public, stable view of the language names this backend can chunk.
TREE_SITTER_LANGUAGES: frozenset[str] = frozenset(_LANGUAGES)

# File extension → language name. ``chunk_file`` routes code files here.
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}

# tree-sitter field name carrying the real definition inside a wrapper node.
_UNWRAP_FIELD: dict[str, str] = {
    "decorated_definition": "definition",
    "export_statement": "declaration",
}


def language_for_path(path: str | None) -> str | None:
    """Return the tree-sitter language name for ``path``, or ``None`` if unknown."""
    if not path:
        return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    if dot <= 0:
        return None
    return _EXT_TO_LANGUAGE.get(name[dot:])


# --------------------------------------------------------------------------- #
# Grammar loading (cached, lazy, degrades to None)                            #
# --------------------------------------------------------------------------- #


@cache
def _load_parser(language: str) -> object | None:
    """Build a cached tree-sitter ``Parser`` for ``language`` (or ``None``).

    Returns ``None`` — never raises — when ``tree_sitter`` or the grammar
    package is not installed, so callers fall back to the ``ast`` path.
    """
    spec = _LANGUAGES.get(language)
    if spec is None:
        return None
    try:
        from tree_sitter import Language, Parser

        grammar_module = importlib.import_module(spec.module)
        grammar = getattr(grammar_module, spec.loader)()
        ts_language = Language(grammar)
        try:
            return Parser(ts_language)
        except TypeError:  # pragma: no cover - older tree-sitter API
            parser = Parser()
            parser.language = ts_language
            return parser
    except (ImportError, AttributeError, ValueError, TypeError):
        return None


def treesitter_available(language: str) -> bool:
    """Whether a working tree-sitter parser exists for ``language`` right now."""
    return _load_parser(language) is not None


# --------------------------------------------------------------------------- #
# Node walking                                                                 #
# --------------------------------------------------------------------------- #


def _unwrap(node: Node) -> Node | None:
    field_name = _UNWRAP_FIELD.get(node.type)
    if field_name is None:
        return None
    return node.child_by_field_name(field_name)


def _is_definition(node: Node, spec: _LanguageSpec) -> bool:
    """Whether ``node`` is a top-level definition worth its own chunk."""
    if node.type in spec.def_types:
        return True
    if node.type in spec.wrapper_types:
        inner = _unwrap(node)
        return inner is not None and (
            inner.type in spec.def_types or inner.type in spec.wrapper_types
        )
    return False


def _symbol_of(node: Node, source: bytes) -> str | None:
    """Best-effort definition name (e.g. ``alpha``, ``Gamma``, ``Add``)."""
    cur: Node | None = node
    for _ in range(5):  # unwrap decorated_definition / export_statement chains
        if cur is None or cur.type not in _UNWRAP_FIELD:
            break
        cur = _unwrap(cur)
    if cur is None:
        return None
    named = cur.child_by_field_name("name")
    if named is not None:
        return source[named.start_byte : named.end_byte].decode("utf-8", "replace")
    # Go puts the name a level down: type_declaration → type_spec → name.
    for child in cur.named_children:
        spec_name = child.child_by_field_name("name")
        if spec_name is not None:
            return source[spec_name.start_byte : spec_name.end_byte].decode("utf-8", "replace")
    return None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_chunk(
    *,
    path: str,
    content: str,
    start_line: int,
    end_line: int,
    symbol: str | None,
    language: str,
    weight: float,
) -> Chunk:
    return Chunk(
        content=content,
        chunk_type=ChunkType.CODE,
        path=path,
        start_line=start_line,
        end_line=end_line,
        language=language,
        symbol=symbol,
        weight=weight,
        content_hash=_hash(content),
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #


def chunk_with_treesitter(
    path: str, src: str, language: str, *, weight: float = 1.0
) -> list[Chunk] | None:
    """Chunk ``src`` with tree-sitter, or return ``None`` to signal fallback.

    Returns ``None`` (not an empty list) when:

    * ``tree_sitter`` or the grammar for ``language`` is unavailable;
    * the parse produced an error tree (the caller should fall back to the
      ``ast`` path / a whole-file chunk — mirroring the ``ast`` chunker's
      "never raises on bad input" contract); or
    * no chunks could be derived from a non-empty source.

    Empty / whitespace-only source yields ``[]`` (parsed cleanly, nothing to do).
    """
    if not src.strip():
        return []
    parser = _load_parser(language)
    if parser is None:
        return None
    spec = _LANGUAGES[language]

    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)  # type: ignore[attr-defined]
    root = tree.root_node
    if root.has_error:
        return None

    lines = src.splitlines()
    chunks: list[Chunk] = []
    run: list[Node] = []

    def flush_run() -> None:
        if not run:
            return
        start = min(node.start_point[0] + 1 for node in run)
        end = max(node.end_point[0] + 1 for node in run)
        content = "\n".join(lines[start - 1 : end])
        if content.strip():
            chunks.append(
                _make_chunk(
                    path=path,
                    content=content,
                    start_line=start,
                    end_line=end,
                    symbol="<module>",
                    language=language,
                    weight=weight,
                )
            )
        run.clear()

    for child in root.children:
        if _is_definition(child, spec):
            flush_run()
            start = child.start_point[0] + 1
            end = child.end_point[0] + 1
            content = "\n".join(lines[start - 1 : end])
            chunks.append(
                _make_chunk(
                    path=path,
                    content=content,
                    start_line=start,
                    end_line=end,
                    symbol=_symbol_of(child, source_bytes),
                    language=language,
                    weight=weight,
                )
            )
        else:
            run.append(child)
    flush_run()

    if not chunks:
        return None

    chunks.sort(key=lambda chunk: chunk.start_line or 0)
    return chunks
