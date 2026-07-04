"""Pure leaderboard ranking + tie-breaks (F35 AC12).

Stable competition ranking: composite DESC, verified before unverified on a
composite tie, then earliest submission first. Entries with equal
``(composite, verified)`` share a rank (1224-style competition ranking).
"""

from __future__ import annotations

from collections.abc import Sequence

from forge_eval.benchmark.models import LeaderboardRow

__all__ = ["rank_submissions"]


def rank_submissions(rows: Sequence[LeaderboardRow]) -> list[LeaderboardRow]:
    """Return a newly ranked copy of ``rows`` (input order does not matter)."""
    ordered = sorted(
        rows,
        key=lambda r: (-r.composite_score, not r.verified, r.submitted_at),
    )
    ranked: list[LeaderboardRow] = []
    previous_key: tuple[float, bool] | None = None
    current_rank = 0
    for position, row in enumerate(ordered, start=1):
        key = (row.composite_score, row.verified)
        if key != previous_key:
            current_rank = position
            previous_key = key
        ranked.append(row.model_copy(update={"rank": current_rank}))
    return ranked
