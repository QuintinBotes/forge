"""f38 observability-cost-metrics: cost_event + model_price

Creates the two F38 cost-ledger tables (slice §3.1):

* ``model_price`` — effective-dated BYOK price book; ``workspace_id IS NULL``
  rows are the global defaults, non-null rows are workspace overrides.
* ``cost_event`` — the durable, append-only token/cost ledger, idempotent on
  the unique ``(workspace_id, request_id)`` index (no double-billing), with the
  generated ``total_tokens`` column and the per-task/per-phase/per-provider
  rollup indexes.

A seed inserts sane global default prices for the providers Forge ships with
(overridable; unknown models price at 0 and surface via
``forge_unpriced_model_total`` — never silently dropped).

Tables are metadata-driven (``create_all`` over the live models) so the
cross-dialect column variants apply automatically, and the upgrade is
idempotent on a fresh metadata-driven chain (mirrors 0019/0020). Downgrade
drops only the two F38 tables.

Revision ID: 0021_f38_cost_ledger
Revises: 0020_f37_auth_secrets
Create Date: 2026-07-04
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0021_f38_cost_ledger"
down_revision: str | None = "0020_f37_auth_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order matters: cost_event FKs model_price.
F38_TABLES = ("model_price", "cost_event")

#: Global default price book (workspace_id NULL). USD per 1k tokens.
SEED_PRICES: tuple[tuple[str, str, str, str, str], ...] = (
    # (provider, model, kind, prompt_usd_per_1k, completion_usd_per_1k)
    ("anthropic", "claude-sonnet-4-5", "completion", "0.003", "0.015"),
    ("anthropic", "claude-haiku-4-5", "completion", "0.001", "0.005"),
    ("anthropic", "claude-opus-4-1", "completion", "0.015", "0.075"),
    ("openai", "gpt-4o", "completion", "0.0025", "0.01"),
    ("openai", "gpt-4o-mini", "completion", "0.00015", "0.0006"),
    ("openai", "text-embedding-3-small", "embedding", "0.00002", "0"),
    ("openai", "text-embedding-3-large", "embedding", "0.00013", "0"),
    ("jina", "jina-reranker-v2-base-multilingual", "rerank", "0.00002", "0"),
)


def _f38_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F38_TABLES if name in by_name]


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    tables = [t for t in _f38_tables() if t.name not in existing]
    if tables:
        Base.metadata.create_all(bind=op.get_bind(), tables=tables)

    # Seed the global defaults whenever the price book has none — covers both a
    # real upgrade from 0020 AND a fresh metadata-driven chain where 0001 already
    # created the (empty) tables. Idempotent: existing global rows short-circuit.
    price_table = Base.metadata.tables["model_price"]
    bind = op.get_bind()
    has_globals = bind.execute(
        sa.select(sa.func.count())
        .select_from(price_table)
        .where(price_table.c.workspace_id.is_(None))
    ).scalar_one()
    if not has_globals:
        now = datetime.now(UTC)
        bind.execute(
            price_table.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "workspace_id": None,
                    "provider": provider,
                    "model": model,
                    "kind": kind,
                    "prompt_usd_per_1k": Decimal(prompt),
                    "completion_usd_per_1k": Decimal(completion),
                    "currency": "USD",
                    "effective_from": now,
                    "created_by": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for provider, model, kind, prompt, completion in SEED_PRICES
            ],
        )


def downgrade() -> None:
    existing = _existing_tables()
    by_name = {t.name: t for t in _f38_tables()}
    # Drop in dependency order: cost_event first (FKs model_price).
    tables = [by_name[n] for n in ("cost_event", "model_price") if n in by_name and n in existing]
    if tables:
        Base.metadata.drop_all(bind=op.get_bind(), tables=tables)
