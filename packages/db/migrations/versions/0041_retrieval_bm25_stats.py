"""retrieval BM25 stats: token counts + incremental corpus statistics

Backs real Okapi BM25 on Postgres via candidate-then-rescore (spec J §2.5,
ADR-0017):

* ``retrieval_chunk.token_count`` — each chunk's token length under the shared
  ``forge_knowledge.text.tokenize`` tokeniser (length normalisation input).
* ``retrieval_term_stats(workspace_id, term, df)`` — per-workspace document
  frequencies (IDF input), maintained by the knowledge store in the same
  transaction as each chunk upsert/delete.
* ``retrieval_workspace_stats(workspace_id, chunk_count, total_tokens)`` —
  per-workspace corpus totals (``N`` and average document length).

The upgrade backfills all three from existing chunks (and, on Postgres,
populates any missing ``tsv``), so rescoring is correct from the first query
after deployment. Purely additive apart from the new defaulted column.

Idempotent like 0038/0039/0040: ``upgrade`` creates/adds only what is missing
(the metadata-driven baseline provisions these objects on a fresh chain, so
this revision must tolerate finding them present); ``downgrade`` drops only
what this revision introduced. The backfill is likewise guarded: token counts
are recomputed only for rows still at the ``0`` default, and the statistics
tables are built only when empty.

Revision ID: 0041_retrieval_bm25_stats
Revises: 0040_self_eval_baseline
Create Date: 2026-11-07
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0041_retrieval_bm25_stats"
down_revision: str | None = "0040_self_eval_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("retrieval_term_stats", "retrieval_workspace_stats")
_CHUNK_TABLE = "retrieval_chunk"
_COLUMN = "token_count"

# Mirrors ``forge_knowledge.text.tokenize`` exactly (lower-case runs of ASCII
# letters/digits/underscores). Duplicated — not imported — so a migration
# never depends on application code that may change after it ships.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(content: str) -> list[str]:
    return _TOKEN_RE.findall(content.lower())


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_CHUNK_TABLE)}


def _owned_tables() -> list[sa.Table]:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in _TABLES if name in by_name]


def _backfill() -> None:
    """Populate token counts + BM25 statistics from existing chunks."""
    bind = op.get_bind()
    chunk = Base.metadata.tables[_CHUNK_TABLE]
    term_stats = Base.metadata.tables["retrieval_term_stats"]
    workspace_stats = Base.metadata.tables["retrieval_workspace_stats"]

    rows = bind.execute(
        sa.select(chunk.c.id, chunk.c.workspace_id, chunk.c.content, chunk.c.token_count)
    ).all()
    if not rows:
        return

    tokens_by_id = {row.id: _tokenize(row.content or "") for row in rows}

    for row in rows:
        if not row.token_count:
            bind.execute(
                chunk.update()
                .where(chunk.c.id == row.id)
                .values(token_count=len(tokens_by_id[row.id]))
            )

    already_built = bind.execute(sa.select(sa.func.count()).select_from(workspace_stats)).scalar()
    if not already_built:
        doc_freq: dict[object, Counter[str]] = defaultdict(Counter)
        chunk_counts: Counter[object] = Counter()
        total_tokens: Counter[object] = Counter()
        for row in rows:
            tokens = tokens_by_id[row.id]
            chunk_counts[row.workspace_id] += 1
            total_tokens[row.workspace_id] += len(tokens)
            doc_freq[row.workspace_id].update(set(tokens))
        bind.execute(
            workspace_stats.insert(),
            [
                {
                    "workspace_id": workspace_id,
                    "chunk_count": chunk_counts[workspace_id],
                    "total_tokens": total_tokens[workspace_id],
                }
                for workspace_id in chunk_counts
            ],
        )
        term_rows = [
            {"workspace_id": workspace_id, "term": term, "df": df}
            for workspace_id, counter in doc_freq.items()
            for term, df in counter.items()
        ]
        if term_rows:
            bind.execute(term_stats.insert(), term_rows)

    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE retrieval_chunk "
                "SET tsv = to_tsvector('english', content) WHERE tsv IS NULL"
            )
        )


def upgrade() -> None:
    existing = _existing_tables()
    to_create = [t for t in _owned_tables() if t.name not in existing]
    if to_create:
        Base.metadata.create_all(bind=op.get_bind(), tables=to_create)

    if _CHUNK_TABLE in existing and _COLUMN not in _existing_columns():
        op.add_column(
            _CHUNK_TABLE,
            sa.Column(
                _COLUMN,
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )

    _backfill()


def downgrade() -> None:
    existing = _existing_tables()

    if _CHUNK_TABLE in existing and _COLUMN in _existing_columns():
        with op.batch_alter_table(_CHUNK_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)

    to_drop = [t for t in reversed(_owned_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=op.get_bind(), tables=to_drop)
