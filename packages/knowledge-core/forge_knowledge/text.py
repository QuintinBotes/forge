"""Shared tokenisation for the Knowledge/RAG retrieval layer.

A single tokeniser keeps the deterministic embedding client (semantic leg) and
the BM25 keyword store (lexical leg) consistent. Identifiers are preserved whole
(underscores and digits are word characters) so an exact symbol like
``compute_rrf_score`` survives as one token — the property the hybrid pipeline's
keyword leg depends on to recover matches the semantic leg dilutes.
"""

from __future__ import annotations

import hashlib
import re

#: Word = run of ASCII letters/digits/underscores (keeps identifiers intact).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, identifiers preserved whole."""
    return _TOKEN_RE.findall(text.lower())


#: English stopwords dropped from *queries* on every dialect (spec J §2.5
#: correction pass): the Postgres ``english`` tsearch stop list, restricted to
#: the plain ``[a-z]+`` forms the tokeniser can emit (the contraction forms
#: in that list — ``i'm``, ``don't`` — never survive tokenisation as one
#: token). Documents keep stopwords: they count toward ``token_count`` and
#: document-length normalisation, and stay in the stored ``tsv``. Sharing one
#: list means a stopword-only query returns no hits on SQLite and Postgres
#: alike instead of the old split (SQLite scored them, the ``english``
#: tsquery dropped them silently).
ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "doing",
        "don",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "s",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "t",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)


#: Distinct query terms kept (the first ones, in query order). Postgres
#: evaluates a tsquery recursively and exceeds its stack at ~15k terms; both
#: dialects apply the same cap so they still answer identically.
MAX_QUERY_TERMS = 64


def query_terms(query: str) -> list[str]:
    """Distinct query tokens with stopwords dropped, order preserved.

    The single query-side token view shared by both BM25 dialects: a
    stopword-only query yields ``[]`` (and therefore no hits) everywhere.
    """
    terms = [term for term in dict.fromkeys(tokenize(query)) if term not in ENGLISH_STOPWORDS]
    return terms[:MAX_QUERY_TERMS]


# -- Postgres lexemes ------------------------------------------------------ #
#
# Candidate generation on Postgres must see exactly the tokens the BM25
# rescorer sees. Every text-search parser rewrites tokens (``english`` stems
# and keeps ``self.retry`` whole; even ``simple`` splits ``retry_count`` at
# the underscore and drops ``__all__``'s edges), so forge writes the tsvector
# and the tsquery itself, as quoted lexemes, and no parser runs.

#: Longest token used verbatim as a lexeme (Postgres refuses lexemes over
#: 2,046 bytes); longer tokens become a stable stand-in on both sides.
_LEXEME_MAX = 200
#: Postgres keeps positions 1..16,383 and at most 256 per lexeme.
_MAX_POSITION = 16_383
_MAX_POSITIONS_PER_LEXEME = 256
#: Stay under Postgres' 1 MB tsvector limit for pathological chunks.
_MAX_TSVECTOR_CHARS = 900_000


def pg_lexeme(token: str) -> str:
    """The lexeme a tokenizer token is stored and queried as (``[a-z0-9_]+``)."""
    if len(token) <= _LEXEME_MAX:
        return token
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"{token[:64]}_{digest}"


def tsvector_literal(tokens: list[str]) -> str:
    """A ``tsvector`` input string holding ``tokens`` exactly, with positions."""
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens, start=1):
        kept = positions.setdefault(pg_lexeme(token), [])
        if len(kept) < _MAX_POSITIONS_PER_LEXEME:
            kept.append(min(index, _MAX_POSITION))
    parts: list[str] = []
    size = 0
    for lexeme, where in positions.items():
        part = f"'{lexeme}':{','.join(map(str, where))}"
        if size + len(part) > _MAX_TSVECTOR_CHARS:
            # Keep every remaining lexeme once rather than dropping words.
            part = f"'{lexeme}'"
            if size + len(part) > _MAX_TSVECTOR_CHARS:
                break
        parts.append(part)
        size += len(part) + 1
    return " ".join(parts)


def tsquery_literal(terms: list[str]) -> str:
    """An OR ``tsquery`` input string matching any of ``terms`` exactly."""
    return " | ".join(f"'{lexeme}'" for lexeme in dict.fromkeys(pg_lexeme(t) for t in terms))


__all__ = [
    "ENGLISH_STOPWORDS",
    "MAX_QUERY_TERMS",
    "pg_lexeme",
    "query_terms",
    "tokenize",
    "tsquery_literal",
    "tsvector_literal",
]
