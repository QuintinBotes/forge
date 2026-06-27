# Pulse

Pulse is an open source incident and task orchestration service for engineering
teams. It tracks tasks through a workflow, escalates incidents to the on-call
rotation, and notifies engineers. It is backed by Postgres with pgvector and
exposes a hybrid semantic plus keyword search over its own codebase.

This directory is a tiny sample repository used by the Forge Knowledge/RAG spine
smoke test: it is indexed end to end and queried to prove attributed, reranked
hybrid retrieval works against a real on-disk source tree.
