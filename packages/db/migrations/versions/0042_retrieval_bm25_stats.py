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

The upgrade backfills all three from existing chunks and, on Postgres,
recomputes *every* row's ``tsv`` from the Python tokeniser's output, written
directly as quoted lexemes with positions (no text-search parser: ``english``
stems and keeps ``self.retry`` whole, and even ``simple`` splits
``retry_count``), with no ``WHERE tsv IS NULL`` guard, since pre-J rows hold
raw-content english vectors and sync never rewrites unchanged chunks.
Candidates and the rescorer therefore see the same tokens from the first
query after deployment.

In the branch that adds ``token_count`` — i.e. only on a pre-0042 schema,
never on a fresh metadata-provisioned chain — the upgrade also NULLs every
existing ``embedding``. **This is deliberate and irreversible**: every pre-J
production indexer hard-wired the deterministic hash embedder, so every
deployed vector is a hash vector, not a semantic one (ADR-0018). Keeping
them would rank real query vectors against noise while reporting the vector
leg "configured". NULLed rows are re-embedded by the store on the next
index/sync once a real embedder is configured (operators need one full sync
after configuring an embedder); until then search runs honestly BM25-only
for those rows. A fresh chain never takes this branch, so real vectors
written by post-J code are never wiped.

The backfill is one keyset-paged loop (2,000 rows by id) in a single
transaction: each row is tokenised once in Python (no SQL regexp — Python
``lower()`` semantics differ), one executemany per page sets ``token_count``
and (Postgres) ``tsv``, only per-workspace aggregates are kept in memory,
terms longer than the 255-char stats column are excluded from the df table
(but still count toward ``token_count``/``total_tokens``, as the store
maintains them), and term rows are inserted in 10k batches. The single
transaction means the ``add_column`` ACCESS EXCLUSIVE lock is held for the
whole O(N) backfill; that is accepted — it keeps the statistics consistent
with the rows they describe and the build-once guard sound — but on a very
large corpus schedule the migration accordingly.

Idempotent like 0038/0039/0040: ``upgrade`` creates/adds only what is missing
(the metadata-driven baseline provisions these objects on a fresh chain, so
this revision must tolerate finding them present); ``downgrade`` drops only
what this revision introduced. The statistics tables are built only when
empty; ``token_count``/``tsv`` are recomputed for every row regardless
(re-running the backfill derives the same values).

The paged loop mirrors ``forge_knowledge.stores.rebuild_bm25_stats`` but is
duplicated here on purpose: a migration must never import application
packages that drift after it ships, so the tokeniser and loop are frozen
copies.

Revision ID: 0042_retrieval_bm25_stats
Revises: 0041_world_model
Create Date: 2026-11-07
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0042_retrieval_bm25_stats"
down_revision: str | None = "0041_world_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("retrieval_term_stats", "retrieval_workspace_stats")
_CHUNK_TABLE = "retrieval_chunk"
_COLUMN = "token_count"

_PAGE_SIZE = 2_000
_TERM_INSERT_BATCH = 10_000
_TERM_MAX_LEN = 255

# Mirrors ``forge_knowledge.text.tokenize`` exactly (lower-case runs of ASCII
# letters/digits/underscores). Duplicated — not imported — so a migration
# never depends on application code that may change after it ships.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(content: str) -> list[str]:
    return _TOKEN_RE.findall(content.lower())


# Mirrors ``forge_knowledge.text.pg_lexeme`` / ``tsvector_literal`` exactly
# (duplicated for the same reason as the tokeniser).
def _lexeme(token: str) -> str:
    if len(token) <= 200:
        return token
    return f"{token[:64]}_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:32]}"


def _tsvector_literal(tokens: list[str]) -> str:
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens, start=1):
        kept = positions.setdefault(_lexeme(token), [])
        if len(kept) < 256:
            kept.append(min(index, 16_383))
    parts: list[str] = []
    size = 0
    for lexeme, where in positions.items():
        part = f"'{lexeme}':{','.join(map(str, where))}"
        if size + len(part) > 900_000:
            part = f"'{lexeme}'"
            if size + len(part) > 900_000:
                break
        parts.append(part)
        size += len(part) + 1
    return " ".join(parts)


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_CHUNK_TABLE)}


def _owned_tables() -> list[sa.Table]:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in _TABLES if name in by_name]


def _backfill() -> None:
    """Populate token counts, tsv and BM25 statistics from existing chunks.

    Keyset pages ordered by id, each fetched with ``.all()`` (never
    ``yield_per``/streaming — interleaving a stream with UPDATEs on SQLite
    can revisit rows and double-count df). Statistics tables are built only
    when empty; the per-row ``token_count``/``tsv`` rewrite always runs.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    chunk = Base.metadata.tables[_CHUNK_TABLE]
    term_stats = Base.metadata.tables["retrieval_term_stats"]
    workspace_stats = Base.metadata.tables["retrieval_workspace_stats"]

    already_built = bind.execute(sa.select(sa.func.count()).select_from(workspace_stats)).scalar()

    # Bindparam names must not equal column names in an executemany UPDATE.
    if is_postgres:
        update_stmt = (
            chunk.update()
            .where(chunk.c.id == sa.bindparam("b_id"))
            .values(
                token_count=sa.bindparam("b_count"),
                tsv=sa.cast(sa.bindparam("b_text"), TSVECTOR),
            )
        )
    else:
        update_stmt = (
            chunk.update()
            .where(chunk.c.id == sa.bindparam("b_id"))
            .values(token_count=sa.bindparam("b_count"))
        )

    doc_freq: dict[object, Counter[str]] = defaultdict(Counter)
    chunk_counts: Counter[object] = Counter()
    total_tokens: Counter[object] = Counter()
    last_id: object = None
    while True:
        stmt = sa.select(chunk.c.id, chunk.c.workspace_id, chunk.c.content)
        if last_id is not None:
            stmt = stmt.where(chunk.c.id > last_id)
        rows = bind.execute(stmt.order_by(chunk.c.id.asc()).limit(_PAGE_SIZE)).all()
        if not rows:
            break
        updates: list[dict[str, object]] = []
        for row in rows:
            tokens = _tokenize(row.content or "")
            entry: dict[str, object] = {"b_id": row.id, "b_count": len(tokens)}
            if is_postgres:
                entry["b_text"] = _tsvector_literal(tokens)
            updates.append(entry)
            if not already_built:
                chunk_counts[row.workspace_id] += 1
                total_tokens[row.workspace_id] += len(tokens)
                doc_freq[row.workspace_id].update(
                    {t for t in set(tokens) if len(t) <= _TERM_MAX_LEN}
                )
        bind.execute(update_stmt, updates)
        last_id = rows[-1].id

    if already_built or not chunk_counts:
        return
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
        for term, df in sorted(counter.items())
    ]
    for start in range(0, len(term_rows), _TERM_INSERT_BATCH):
        bind.execute(term_stats.insert(), term_rows[start : start + _TERM_INSERT_BATCH])


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
        # Pre-0042 schema => every stored embedding is a pre-J hash vector.
        # NULL them (irreversible; see module docstring) so the store
        # re-embeds with the configured real embedder on the next sync.
        op.execute("UPDATE retrieval_chunk SET embedding = NULL WHERE embedding IS NOT NULL")

    _backfill()


def downgrade() -> None:
    existing = _existing_tables()

    if _CHUNK_TABLE in existing and _COLUMN in _existing_columns():
        with op.batch_alter_table(_CHUNK_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)

    to_drop = [t for t in reversed(_owned_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=op.get_bind(), tables=to_drop)
