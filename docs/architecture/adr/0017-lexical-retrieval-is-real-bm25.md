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

Amended 2026-11-08: candidate generation uses the `simple` text-search config over the tokenizer's
own output (stored `tsv` and query alike), not `english`, and both dialects drop one shared English
stopword list from queries. The `english` parser kept dotted names, paths and versions as single
lexemes, stemmed and dropped stopwords, so Postgres missed chunks the rescorer would score and the
two dialects disagreed. Statistics are applied with additive upserts so concurrent indexers in one
workspace cannot lose increments.

## Consequences

- Real BM25 ranking with no new dependency; recall is bounded by the 200-candidate pool, which is
  measured by the retrieval benchmark.
- Indexing writes term statistics in the same transaction as each chunk; a backfill covers existing
  chunks.
- Revisit pg_search if the candidate cap measurably limits recall.
- `simple` splits identifiers at interior underscores, so `retry` also matches `retry_helper`
  chunks; the rescore scores them 0. Only a flood of more than 200 such chunks outranking an exact
  match on `ts_rank_cd` could evict it; the candidate cap is the same bound as above.
- Operators can rebuild a workspace's statistics with `forge-cli knowledge rebuild-bm25-stats`.
