"""Unit tests for canonical content hashing (echo suppression + conflict)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from forge_contracts.pm import (
    ExternalTask,
    ForgePriority,
    ForgeTask,
    PMProvider,
    StatusCategory,
)
from forge_integrations.pm.hashing import external_content_hash, forge_content_hash


def _forge(**overrides) -> ForgeTask:
    base = {
        "id": uuid4(),
        "key": "TASK-1",
        "project_id": uuid4(),
        "title": "Title",
        "description_md": "Body",
        "status_category": StatusCategory.started,
        "priority": ForgePriority.high,
        "assignee_email": "a@acme.test",
        "label_names": ["b", "a"],
        "version": 3,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ForgeTask(**base)


def _external(**overrides) -> ExternalTask:
    base = {
        "provider": PMProvider.jira,
        "external_id": "10001",
        "external_key": "ENG-1",
        "url": "https://x/browse/ENG-1",
        "title": "Title",
        "description_md": "Body",
        "status_name": "In Progress",
        "status_category": StatusCategory.started,
        "priority_token": "High",
        "assignee_email": "a@acme.test",
        "labels": ["a", "b"],
        "external_updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ExternalTask(**base)


def test_forge_content_hash_stable_and_field_scoped() -> None:
    t1 = _forge(version=1, updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = _forge(version=99, updated_at=datetime(2030, 1, 1, tzinfo=UTC))
    # version/updated_at must NOT affect the hash.
    assert forge_content_hash(t1) == forge_content_hash(t2)
    # label order must NOT matter.
    assert forge_content_hash(_forge(label_names=["a", "b"])) == forge_content_hash(
        _forge(label_names=["b", "a"])
    )
    # a real field change DOES change the hash.
    assert forge_content_hash(_forge(title="Other")) != forge_content_hash(_forge())


def test_external_content_hash_stable() -> None:
    e1 = _external(external_updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    e2 = _external(external_updated_at=datetime(2031, 1, 1, tzinfo=UTC))
    assert external_content_hash(e1) == external_content_hash(e2)
    assert external_content_hash(_external(title="Other")) != external_content_hash(
        _external()
    )


def test_hash_excludes_secrets_and_raw() -> None:
    # The provider `raw` blob (where secrets could leak) must not feed the hash.
    e1 = _external(raw={})
    e2 = _external(raw={"token": "super-secret", "authorization": "Bearer x"})
    assert external_content_hash(e1) == external_content_hash(e2)


def test_hash_is_hex_sha256() -> None:
    h = forge_content_hash(_forge())
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_external_updated_at_excluded_even_with_skew() -> None:
    now = datetime.now(UTC)
    assert external_content_hash(_external(external_updated_at=now)) == (
        external_content_hash(_external(external_updated_at=now + timedelta(days=10)))
    )
