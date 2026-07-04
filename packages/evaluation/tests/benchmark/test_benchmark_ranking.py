"""F35 unit tests — ranking & tie-breaks (AC12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from forge_eval.benchmark import LeaderboardRow, rank_submissions

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _row(
    composite: float,
    *,
    verified: bool = True,
    minutes: int = 0,
    label: str = "model",
) -> LeaderboardRow:
    return LeaderboardRow(
        rank=0,
        submission_id=uuid.uuid4(),
        model_label=label,
        agent_mode="single_agent",
        composite_score=composite,
        verified=verified,
        forge_version="3.0.0",
        submitter_name="tester",
        per_category=[],
        submitted_at=T0 + timedelta(minutes=minutes),
    )


def test_rank_orders_by_composite_desc() -> None:
    rows = [_row(0.5, label="mid"), _row(0.9, label="top"), _row(0.1, label="low")]
    ranked = rank_submissions(rows)
    assert [r.model_label for r in ranked] == ["top", "mid", "low"]
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_verified_outranks_unverified_on_tie() -> None:
    rows = [
        _row(0.8, verified=False, label="unverified"),
        _row(0.8, verified=True, label="verified"),
    ]
    ranked = rank_submissions(rows)
    assert [r.model_label for r in ranked] == ["verified", "unverified"]
    # Different (composite, verified) keys -> distinct competition ranks.
    assert [r.rank for r in ranked] == [1, 2]


def test_earliest_submission_wins_full_tie() -> None:
    rows = [
        _row(0.8, minutes=30, label="later"),
        _row(0.8, minutes=0, label="earlier"),
    ]
    ranked = rank_submissions(rows)
    assert [r.model_label for r in ranked] == ["earlier", "later"]
    # Equal (composite, verified) share a competition rank.
    assert [r.rank for r in ranked] == [1, 1]


def test_competition_ranking_skips_after_shared_rank() -> None:
    rows = [
        _row(0.9, label="a"),
        _row(0.8, minutes=0, label="b1"),
        _row(0.8, minutes=1, label="b2"),
        _row(0.7, label="c"),
    ]
    ranked = rank_submissions(rows)
    assert [r.rank for r in ranked] == [1, 2, 2, 4]


def test_input_not_mutated_and_rank_one_based() -> None:
    rows = [_row(0.4), _row(0.6)]
    ranked = rank_submissions(rows)
    assert all(r.rank == 0 for r in rows)
    assert ranked[0].rank == 1
