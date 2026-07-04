# ruff: noqa: F821 - illustrative sample snippet (undefined helpers) for RAG indexing
"""Hybrid retrieval over the Pulse knowledge base."""


def hybrid_search(query, k=10):
    # reciprocal rank fusion combines the dense semantic and the keyword rankings
    # then a cross encoder reranker reorders the candidates before returning top k
    fused = fuse(semantic(query), keyword(query))
    return rerank(query, fused, top_n=k)
