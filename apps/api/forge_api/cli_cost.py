"""``forge cost`` CLI (F38 Journey G): reprice, price set, summary.

Operates directly against the configured database (``--database-url`` or
``FORGE_DATABASE_URL``) through the same ``forge_obs.cost`` repositories the
API and worker use, so the CLI can never disagree with the product surface.
``compute`` is a pure offline helper (price math sanity checks).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import compute_cost

__all__ = ["build_parser", "main"]


def _session_factory(database_url: str | None):
    from forge_db.session import create_db_engine, create_session_factory

    url = database_url or os.environ.get("FORGE_DATABASE_URL")
    if not url:
        print("no database configured: pass --database-url or set FORGE_DATABASE_URL",
              file=sys.stderr)
        return None
    return create_session_factory(create_db_engine(url))


def _cmd_compute(args: argparse.Namespace) -> int:
    usage = ModelUsage(
        workspace_id=uuid.UUID(int=0),
        request_id="cli",
        provider="cli",
        model="cli",
        prompt_tokens=args.prompt_tokens,
        completion_tokens=args.completion_tokens,
        occurred_at=datetime.now(UTC),
    )
    price = ModelPrice(
        provider="cli",
        model="cli",
        prompt_usd_per_1k=Decimal(args.prompt_usd_per_1k),
        completion_usd_per_1k=Decimal(args.completion_usd_per_1k),
        effective_from=datetime.now(UTC),
    )
    print(f"cost_usd={compute_cost(usage, price)}")
    return 0


def _cmd_reprice(args: argparse.Namespace) -> int:
    factory = _session_factory(args.database_url)
    if factory is None:
        return 3
    from forge_db.models import AuditLog
    from forge_obs.cost.pricing import DbPriceBook
    from forge_obs.cost.repository import SqlCostLedger

    workspace_id = uuid.UUID(args.workspace)
    since = datetime.fromisoformat(args.from_iso)
    updated = SqlCostLedger(factory).reprice(
        workspace_id=workspace_id,
        since=since,
        provider=args.provider,
        model=args.model,
        price_book=DbPriceBook(factory),
    )
    # Immutable audit record (F39 contract) — same shape the worker task writes.
    with factory() as session:
        session.add(
            AuditLog(
                workspace_id=workspace_id,
                action="cost.repriced",
                actor_type="system",
                target_type="cost_event",
                details={
                    "since": since.isoformat(),
                    "provider": args.provider,
                    "model": args.model,
                    "updated": updated,
                },
            )
        )
        session.commit()
    print(f"repriced {updated} cost_event row(s)")
    return 0


def _cmd_price_set(args: argparse.Namespace) -> int:
    factory = _session_factory(args.database_url)
    if factory is None:
        return 3
    from forge_db.models.cost import ModelPrice as ModelPriceRow
    from forge_db.models.enums import CostEventKind

    with factory() as session:
        row = ModelPriceRow(
            workspace_id=uuid.UUID(args.workspace) if args.workspace else None,
            provider=args.provider,
            model=args.model,
            kind=CostEventKind(args.kind),
            prompt_usd_per_1k=Decimal(args.prompt_usd_per_1k),
            completion_usd_per_1k=Decimal(args.completion_usd_per_1k),
            effective_from=(
                datetime.fromisoformat(args.effective_from)
                if args.effective_from
                else datetime.now(UTC)
            ),
        )
        session.add(row)
        session.commit()
        print(f"price {row.id} set: {args.provider}/{args.model} ({args.kind})")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    factory = _session_factory(args.database_url)
    if factory is None:
        return 3
    from forge_obs.cost.repository import SqlCostReader

    summary = SqlCostReader(factory).summary(
        workspace_id=uuid.UUID(args.workspace),
        scope=args.scope,
        scope_id=uuid.UUID(args.scope_id) if args.scope_id else uuid.UUID(args.workspace),
        group_by=args.group_by,
        frm=None,
        to=None,
    )
    print(
        f"total=${summary.total_cost_usd} prompt_tokens={summary.total_prompt_tokens} "
        f"completion_tokens={summary.total_completion_tokens}"
    )
    for bucket in summary.buckets:
        print(f"  {bucket.key}: ${bucket.cost_usd}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge cost")
    parser.add_argument("--database-url", dest="database_url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    comp = sub.add_parser("compute", help="offline price math check")
    comp.add_argument("--prompt-tokens", type=int, default=0)
    comp.add_argument("--completion-tokens", type=int, default=0)
    comp.add_argument("--prompt-usd-per-1k", default="0")
    comp.add_argument("--completion-usd-per-1k", default="0")
    comp.set_defaults(func=_cmd_compute)

    rep = sub.add_parser("reprice", help="re-price historical cost events (audited)")
    rep.add_argument("--workspace", required=True)
    rep.add_argument("--from", dest="from_iso", required=True)
    rep.add_argument("--provider", default=None)
    rep.add_argument("--model", default=None)
    rep.set_defaults(func=_cmd_reprice)

    price = sub.add_parser("price-set", help="add a price row (global or workspace)")
    price.add_argument("--workspace", default=None, help="omit for a global default row")
    price.add_argument("--provider", required=True)
    price.add_argument("--model", required=True)
    price.add_argument("--kind", default="completion",
                       choices=["completion", "embedding", "rerank"])
    price.add_argument("--prompt-usd-per-1k", required=True)
    price.add_argument("--completion-usd-per-1k", required=True)
    price.add_argument("--effective-from", default=None)
    price.set_defaults(func=_cmd_price_set)

    summ = sub.add_parser("summary", help="print a scope's cost summary")
    summ.add_argument("--workspace", required=True)
    summ.add_argument("--scope", default="workspace",
                      choices=["workspace", "project", "task"])
    summ.add_argument("--scope-id", dest="scope_id", default=None)
    summ.add_argument("--group-by", dest="group_by", default="provider",
                      choices=["phase", "provider", "model", "none"])
    summ.set_defaults(func=_cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
