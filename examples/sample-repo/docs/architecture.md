# Pulse Architecture

Pulse is composed of a FastAPI service, a Celery background worker, and a
Postgres database with the pgvector extension enabled. Retrieval is always
hybrid: a dense vector semantic leg and a BM25 keyword leg are combined with
reciprocal rank fusion and then reordered by a cross encoder reranker before the
top results are returned with full source attribution.
