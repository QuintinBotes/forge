"""Outcome feedback and effective confidence (spec J §2.2/§2.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import T0, WS, WS2, FakeClock, make_fact
from sqlalchemy.orm import Session

from forge_db.models import AgentRun
from forge_world import (
    FeedbackOutcome,
    Layer,
    SourceKind,
    StatementInput,
    WorldModelService,
    WorldValidationError,
    effective_confidence,
)

PERSON = SourceKind.PERSON


# --- the pure formula ------------------------------------------------------- #


def test_memory_decays_with_a_thirty_day_half_life() -> None:
    fact = make_fact(subject=uuid.uuid4(), layer=Layer.MEMORY, confidence=0.8, recorded_at=T0)
    assert effective_confidence(fact, now=T0) == pytest.approx(0.8)
    assert effective_confidence(fact, now=T0 + timedelta(days=30)) == pytest.approx(0.4)
    assert effective_confidence(fact, now=T0 + timedelta(days=60)) == pytest.approx(0.2)


def test_other_layers_do_not_decay() -> None:
    fact = make_fact(subject=uuid.uuid4(), layer=Layer.TYPED, confidence=0.8, recorded_at=T0)
    assert effective_confidence(fact, now=T0 + timedelta(days=365)) == pytest.approx(0.8)


def test_feedback_multipliers_and_cap() -> None:
    fact = make_fact(subject=uuid.uuid4(), layer=Layer.OPEN, confidence=0.5, recorded_at=T0)
    later = T0 + timedelta(days=10)
    assert effective_confidence(fact, reinforced=1, now=later) == pytest.approx(0.55)
    assert effective_confidence(fact, reinforced=2, weakened=1, now=later) == pytest.approx(
        0.5 * 1.1**2 * 0.8
    )
    capped = make_fact(subject=uuid.uuid4(), layer=Layer.OPEN, confidence=0.99, recorded_at=T0)
    assert effective_confidence(capped, reinforced=5, now=later) == 1.0


def test_memory_decay_and_feedback_compose() -> None:
    fact = make_fact(subject=uuid.uuid4(), layer=Layer.MEMORY, confidence=0.8, recorded_at=T0)
    value = effective_confidence(fact, weakened=1, now=T0 + timedelta(days=30))
    assert value == pytest.approx(0.8 * 0.5 * 0.8)


def test_negative_counts_are_refused() -> None:
    fact = make_fact(subject=uuid.uuid4(), layer=Layer.OPEN, recorded_at=T0)
    with pytest.raises(WorldValidationError):
        effective_confidence(fact, reinforced=-1, now=T0)


# --- service + repository ---------------------------------------------------- #


def _run(session: Session, workspace_id: uuid.UUID) -> uuid.UUID:
    run = AgentRun(workspace_id=workspace_id)
    session.add(run)
    session.flush()
    return run.id


def _fact(
    service: WorldModelService,
    workspace_id: uuid.UUID,
    *,
    subject: str = "Acme",
    value: str = "Berlin",
    layer: Layer | None = None,
    confidence: float = 0.5,
) -> uuid.UUID:
    result = service.assert_input(
        workspace_id,
        StatementInput(
            phrase="is based in",
            subject_name=subject,
            value=value,
            layer=layer,
            confidence=confidence,
        ),
        source_kind=PERSON,
        actor="u1",
    )
    return result.fact.id


def test_record_feedback_counts_and_idempotency(
    service: WorldModelService, session: Session
) -> None:
    fact_id = _fact(service, WS)
    run_id = _run(session, WS)
    session.commit()

    written = service.record_feedback(
        WS, run_id=run_id, fact_ids=[fact_id], outcome=FeedbackOutcome.REINFORCED
    )
    session.commit()
    assert written == 1
    # A replay of the same outcome writes nothing new.
    again = service.record_feedback(
        WS, run_id=run_id, fact_ids=[fact_id], outcome=FeedbackOutcome.WEAKENED
    )
    session.commit()
    assert again == 0
    assert service.feedback_counts(WS, [fact_id]) == {fact_id: (1, 0)}
    rows = service.list_feedback(WS, run_id=run_id)
    assert [(row.fact_id, row.outcome) for row in rows] == [(fact_id, FeedbackOutcome.REINFORCED)]


def test_record_feedback_skips_foreign_and_missing_facts(
    service: WorldModelService, session: Session
) -> None:
    foreign_fact = _fact(service, WS2, subject="Globex", value="Paris")
    run_id = _run(session, WS)
    session.commit()
    written = service.record_feedback(
        WS,
        run_id=run_id,
        fact_ids=[foreign_fact, uuid.uuid4()],
        outcome=FeedbackOutcome.REINFORCED,
    )
    session.commit()
    assert written == 0
    assert service.list_feedback(WS) == []


def test_effective_confidences_move_with_feedback_and_age(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    memory_id = _fact(service, WS, value="Munich", layer=Layer.MEMORY, confidence=0.8)
    plain_id = _fact(service, WS, subject="Globex", value="Paris", confidence=0.5)
    run_id = _run(session, WS)
    session.commit()
    facts = {fact.id: fact for fact in service.query_facts(WS, limit=50)}
    before = service.effective_confidences(WS, list(facts.values()))
    assert before[memory_id] == pytest.approx(0.8)
    assert before[plain_id] == pytest.approx(0.5)

    service.record_feedback(
        WS, run_id=run_id, fact_ids=[plain_id], outcome=FeedbackOutcome.REINFORCED
    )
    session.commit()
    clock.tick(days=30)
    after = service.effective_confidences(WS, list(facts.values()))
    assert after[plain_id] == pytest.approx(0.55)  # reinforced, no decay
    assert after[memory_id] == pytest.approx(0.4)  # one half-life of decay
    # The stored confidence never moves.
    assert service.get_fact(WS, memory_id).confidence == 0.8
    assert datetime.now(UTC) is not None  # (clock is fake; nothing here uses wall time)
