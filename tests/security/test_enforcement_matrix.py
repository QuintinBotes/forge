"""HARD-09 — the enforcement-matrix regression suite.

``security/enforcement-matrix.yaml`` is the source of truth: every row names a
check callable in :mod:`matrix_checks` (offline) or one of the live-db tests in
this module. Adding a control = adding a row + a check; a row whose check is
missing (or vice versa) fails the suite, so the matrix can never silently rot.

The committed evidence rendering (docs/security/evidence/enforcement-matrix.md)
is asserted in sync with the YAML (regenerate with
``uv run python scripts/security/gen_matrix_evidence.py``).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "security" / "enforcement-matrix.yaml"
EVIDENCE_PATH = REPO_ROOT / "docs" / "security" / "evidence" / "enforcement-matrix.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix_checks import OFFLINE_CHECKS  # noqa: E402

pytestmark = pytest.mark.security

#: live-db rows are implemented as fixture-driven tests in this module.
LIVE_DB_CHECKS = {"check_audit_immutability_trigger", "check_pg_vault_roundtrip"}


def _load_matrix() -> list[dict[str, Any]]:
    return yaml.safe_load(MATRIX_PATH.read_text())["controls"]


MATRIX = _load_matrix()
OFFLINE_ROWS = [row for row in MATRIX if row["mode"] == "offline"]
LIVE_ROWS = [row for row in MATRIX if row["mode"] == "live-db"]


def test_matrix_schema_is_complete() -> None:
    seen_ids: set[str] = set()
    for row in MATRIX:
        for key in ("id", "title", "spec", "mode", "check", "asserts"):
            assert row.get(key), f"matrix row {row.get('id')!r} is missing {key!r}"
        assert row["mode"] in {"offline", "live-db"}, row["id"]
        assert row["id"] not in seen_ids, f"duplicate control id {row['id']!r}"
        seen_ids.add(row["id"])


def test_every_offline_row_has_a_check_and_vice_versa() -> None:
    row_checks = {row["check"] for row in OFFLINE_ROWS}
    known = set(OFFLINE_CHECKS)
    missing = row_checks - known
    assert not missing, f"matrix rows with no implementation: {sorted(missing)}"
    orphans = known - row_checks
    assert not orphans, f"checks not bound to a matrix row: {sorted(orphans)}"
    live_checks = {row["check"] for row in LIVE_ROWS}
    assert live_checks == LIVE_DB_CHECKS


@pytest.mark.parametrize("row", OFFLINE_ROWS, ids=lambda r: r["id"])
def test_control(row: dict[str, Any]) -> None:
    """Assert one enforcement-matrix control on the wired path."""
    OFFLINE_CHECKS[row["check"]]()


def test_evidence_doc_is_in_sync() -> None:
    spec = REPO_ROOT / "scripts" / "security" / "gen_matrix_evidence.py"
    import importlib.util

    module_spec = importlib.util.spec_from_file_location("gen_matrix_evidence", spec)
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    expected = module.render(MATRIX)
    assert EVIDENCE_PATH.exists(), "evidence rendering is missing"
    assert EVIDENCE_PATH.read_text() == expected, (
        "docs/security/evidence/enforcement-matrix.md is stale — regenerate with "
        "`uv run python scripts/security/gen_matrix_evidence.py`"
    )


# --------------------------------------------------------------------------- #
# live-db rows (skip cleanly without FORGE_TEST_DATABASE_URL — shared HARD-01) #
# --------------------------------------------------------------------------- #


@pytest.mark.postgres
class TestLiveDbRows:
    @pytest.fixture()
    def factory(self, pg_engine):
        from sqlalchemy.orm import Session, sessionmaker

        from forge_db.base import Base

        Base.metadata.create_all(pg_engine)
        try:
            yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
        finally:
            Base.metadata.drop_all(pg_engine)

    @pytest.fixture()
    def workspace_id(self, factory) -> uuid.UUID:
        from forge_db.models import Workspace

        ws_id = uuid.uuid4()
        with factory() as session:
            session.add(Workspace(id=ws_id, name="SecMatrix", slug=f"sec-{uuid.uuid4().hex[:8]}"))
            session.commit()
        return ws_id

    def test_audit_immutability_trigger(self, factory, workspace_id) -> None:
        """Matrix row `audit-immutability-trigger` (check_audit_immutability_trigger)."""
        from sqlalchemy import delete, update
        from sqlalchemy.exc import DBAPIError

        from forge_contracts.audit import AuditEvent
        from forge_db.audit.writer import SqlAuditWriter
        from forge_db.models import AuditLog

        with factory() as session:
            SqlAuditWriter(session).emit(AuditEvent(workspace_id=workspace_id, action="tool.call"))
            session.commit()

        with factory() as session:
            with pytest.raises(DBAPIError):
                session.execute(
                    update(AuditLog.__table__)
                    .where(AuditLog.__table__.c.workspace_id == workspace_id)
                    .values(result="forged")
                )
            session.rollback()
            with pytest.raises(DBAPIError):
                session.execute(
                    delete(AuditLog.__table__).where(
                        AuditLog.__table__.c.workspace_id == workspace_id
                    )
                )
            session.rollback()

    def test_pg_vault_roundtrip(self, factory, workspace_id) -> None:
        """Matrix row `pg-vault-roundtrip` (check_pg_vault_roundtrip)."""
        from sqlalchemy import select

        from forge_api.auth.crypto import FernetCipher, generate_key
        from forge_db.models import APIKey
        from forge_db.models.enums import APIKeyKind

        cipher = FernetCipher(generate_key())
        plaintext = "sk-live-roundtrip-under-real-cipher"
        with factory() as session:
            session.add(
                APIKey(
                    workspace_id=workspace_id,
                    name="byok",
                    kind=APIKeyKind.MODEL_PROVIDER,
                    encrypted_secret=cipher.encrypt(plaintext),
                )
            )
            session.commit()

        with factory() as session:
            row = session.execute(
                select(APIKey).where(APIKey.workspace_id == workspace_id)
            ).scalar_one()
            assert row.encrypted_secret != plaintext.encode()
            assert plaintext not in repr(row)
            assert cipher.decrypt(row.encrypted_secret) == plaintext
