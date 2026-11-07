# 0018. Retrieval is configured or explicitly absent, never faked

**Status:** Accepted
**Date:** 2026-11-07

## Context

The knowledge router hard-wires `DeterministicEmbeddingClient`, a hash-based embedder, and the
reranker defaults to a fixture. Search results therefore look semantic when they are not, which
ADR-0007 forbids. Lore falls back to keyword-only search silently, and its vector leg can vanish
without an error (lore #2386).

## Decision

- Production uses a configured embedder (`HttpEmbeddingClient` or a local sentence-transformer) and an
  explicitly chosen reranker (`none` by default).
- Without an embedder, search is BM25-only and says so in every response (`legs`, `vector_leg:
  "unconfigured"`); routes that need vectors return 503; indexing stores chunks without vectors.
- The hash embedder and the fixture reranker are constructed only in tests, enforced by a repository
  test.

## Consequences

- A fresh install still searches, honestly, by keywords.
- An embedding dimension that does not match the vector column is refused at startup instead of
  failing on the first insert.
