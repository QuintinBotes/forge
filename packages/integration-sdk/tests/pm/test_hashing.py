"""Unit tests for canonical content hashing (echo suppression + conflict)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from scf_contracts.pm import (
    ExternalTask,
    PMProvider,
    ScfPriority,
    ScfTask,
    StatusCategory,
)
from scf_integrations.pm.hashing import external_content_hash, scf_content_hash


def _scf(**overrides) -> ScfTask:
    base = {
        "id": uuid4(),
        "key": "TASK-1",
        "project_id": uuid4(),
        "title": "Title",
        "description_md": "Body",
        "status_category": StatusCategory.started,
        "priority": ScfPriority.high,
        "assignee_email": "a@acme.test",
        "label_names": ["b", "a"],
        "version": 3,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ScfTask(**base)


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


def test_scf_content_hash_stable_and_field_scoped() -> None:
    t1 = _scf(version=1, updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = _scf(version=99, updated_at=datetime(2030, 1, 1, tzinfo=UTC))
    # version/updated_at must NOT affect the hash.
    assert scf_content_hash(t1) == scf_content_hash(t2)
    # label order must NOT matter.
    assert scf_content_hash(_scf(label_names=["a", "b"])) == scf_content_hash(
        _scf(label_names=["b", "a"])
    )
    # a real field change DOES change the hash.
    assert scf_content_hash(_scf(title="Other")) != scf_content_hash(_scf())


def test_external_content_hash_stable() -> None:
    e1 = _external(external_updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    e2 = _external(external_updated_at=datetime(2031, 1, 1, tzinfo=UTC))
    assert external_content_hash(e1) == external_content_hash(e2)
    assert external_content_hash(_external(title="Other")) != external_content_hash(_external())


def test_hash_excludes_secrets_and_raw() -> None:
    # The provider `raw` blob (where secrets could leak) must not feed the hash.
    e1 = _external(raw={})
    e2 = _external(raw={"token": "super-secret", "authorization": "Bearer x"})
    assert external_content_hash(e1) == external_content_hash(e2)


def test_hash_is_hex_sha256() -> None:
    h = scf_content_hash(_scf())
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_external_updated_at_excluded_even_with_skew() -> None:
    now = datetime.now(UTC)
    assert external_content_hash(_external(external_updated_at=now)) == (
        external_content_hash(_external(external_updated_at=now + timedelta(days=10)))
    )
