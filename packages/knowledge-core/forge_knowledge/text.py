"""Shared tokenisation for the Knowledge/RAG retrieval layer.

A single tokeniser keeps the deterministic embedding client (semantic leg) and
the BM25 keyword store (lexical leg) consistent. Identifiers are preserved whole
(underscores and digits are word characters) so an exact symbol like
``compute_rrf_score`` survives as one token — the property the hybrid pipeline's
keyword leg depends on to recover matches the semantic leg dilutes.
"""

from __future__ import annotations

import re

#: Word = run of ASCII letters/digits/underscores (keeps identifiers intact).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, identifiers preserved whole."""
    return _TOKEN_RE.findall(text.lower())


__all__ = ["tokenize"]
