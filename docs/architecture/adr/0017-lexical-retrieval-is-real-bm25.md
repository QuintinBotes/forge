# 0017. Lexical retrieval is real BM25 on Postgres, without a new dependency

**Status:** Accepted
**Date:** 2026-11-07

## Context

`forge_knowledge.stores.Bm25Store` ranks with Okapi BM25 on SQLite but with `ts_rank` on Postgres,
so production's "BM25" leg is not BM25, the flaw lore's own documentation records about lore.
`plainto_tsquery` also ANDs the terms, which drops recall BM25 would keep. Real BM25 inside Postgres
(pg_search / ParadeDB) needs a new extension, a new database image and a licence review, and forge's
CI and infrastructure paths would all change.

## Decision

Candidate-then-rescore: an OR tsquery over the query terms picks up to 200 candidates by
`ts_rank_cd`; one shared pure `bm25_score` function rescores them with exact Okapi BM25 using
document frequencies and lengths that indexing maintains incrementally in Postgres. SQLite and
Postgres rank identically on the same corpus.

## Consequences

- Real BM25 ranking with no new dependency; recall is bounded by the 200-candidate pool, which is
  measured by the retrieval benchmark.
- Indexing writes term statistics in the same transaction as each chunk; a backfill covers existing
  chunks.
- Revisit pg_search if the candidate cap measurably limits recall.
