"""``forge cost`` CLI round-trip (F38 Journey G) against a file-backed SQLite DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.cli_cost import main
from forge_db.base import Base
from forge_db.models import AuditLog, Workspace
from forge_db.models.cost import CostEvent, ModelPrice
from forge_obs.cost.models import ModelUsage
from forge_obs.cost.repository import SqlCostLedger

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_compute_is_pure_and_exact(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "compute",
            "--prompt-tokens", "2000",
            "--completion-tokens", "500",
            "--prompt-usd-per-1k", "0.003",
            "--completion-usd-per-1k", "0.015",
        ]
    )
    assert code == 0
    assert "cost_usd=0.01350000" in capsys.readouterr().out


def test_db_commands_require_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_DATABASE_URL", raising=False)
    code = main(["summary", "--workspace", str(uuid.uuid4())])
    assert code == 3
    assert "no database configured" in capsys.readouterr().err


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'cost-cli.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def seeded(db_url: str) -> dict:
    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.commit()
        ws_id = ws.id
    SqlCostLedger(factory).upsert_event(
        ModelUsage(
            workspace_id=ws_id,
            request_id="cli-r1",
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_tokens=1000,
            completion_tokens=100,
            occurred_at=NOW,
            phase="executing",
        ),
        cost=Decimal("0.5"),
        price_id=None,
    )
    engine.dispose()
    return {"ws": ws_id, "url": db_url, "factory": factory}


def test_price_set_reprice_summary_roundtrip(
    seeded: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    url, ws = seeded["url"], seeded["ws"]

    assert (
        main(
            [
                "--database-url", url,
                "price-set",
                "--provider", "anthropic",
                "--model", "claude-sonnet-4-5",
                "--kind", "completion",
                "--prompt-usd-per-1k", "0.001",
                "--completion-usd-per-1k", "0.001",
                "--effective-from", (NOW - timedelta(days=1)).isoformat(),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database-url", url,
                "reprice",
                "--workspace", str(ws),
                "--from", (NOW - timedelta(days=1)).isoformat(),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "repriced 1 cost_event row(s)" in out

    assert main(["--database-url", url, "summary", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "total=$0.00110000" in out
    assert "anthropic: $0.00110000" in out

    factory = seeded["factory"]
    with factory() as session:
        assert session.scalars(select(ModelPrice)).all()  # price row landed
        row = session.scalars(select(CostEvent)).one()
        assert Decimal(str(row.cost_usd)) == Decimal("0.0011")
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "cost.repriced")
        ).one()
        assert audit.details["updated"] == 1
