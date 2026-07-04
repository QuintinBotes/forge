"""Command / evidence / manual check runners + predicates (AC7, AC8)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from forge_eval.release.checks import (
    redact,
    run_command_check,
    run_evidence_check,
    run_manual_check,
)
from forge_eval.release.model import Status


# --------------------------- command runner ---------------------------- #
def test_command_exit_zero_is_green() -> None:
    status, _ = run_command_check(
        {"run": f'{sys.executable} -c "import sys; sys.exit(0)"'}, timeout_seconds=30
    )
    assert status is Status.GREEN


def test_command_nonzero_is_red_with_redacted_tail() -> None:
    status, detail = run_command_check(
        {"run": f"{sys.executable} -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\""},
        timeout_seconds=30,
    )
    assert status is Status.RED
    assert "exit 3" in detail


def test_missing_required_env_skips_without_running(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    status, detail = run_command_check(
        {
            "run": f"{sys.executable} -c \"open(r'{marker}','w').write('x')\"",
            "required_env": ["FORGE_DEFINITELY_UNSET_ENV_XYZ"],
        },
        timeout_seconds=30,
    )
    assert status is Status.SKIPPED_NO_CREDS
    assert "FORGE_DEFINITELY_UNSET_ENV_XYZ" in detail
    assert not marker.exists()  # command must NOT have run


def test_command_timeout_is_red() -> None:
    status, detail = run_command_check(
        {"run": f'{sys.executable} -c "import time; time.sleep(10)"'},
        timeout_seconds=1,
    )
    assert status is Status.RED
    assert "timed out" in detail


def test_redact_masks_secret_env_values() -> None:
    env = {"FORGE_SECRET_KEY": "supersecretvalue", "PLAIN": "visible"}
    out = redact("leaked supersecretvalue and visible", env)
    assert "supersecretvalue" not in out
    assert "visible" in out


# --------------------------- evidence predicates ---------------------------- #
def test_evidence_exists_and_missing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    ok, _ = run_evidence_check(
        {"artifact": "a.txt", "predicate": {"type": "exists"}}, root=tmp_path
    )
    assert ok is Status.GREEN
    missing, detail = run_evidence_check(
        {"artifact": "nope.txt", "predicate": {"type": "exists"}}, root=tmp_path
    )
    assert missing is Status.MISSING_EVIDENCE
    assert "missing" in detail


def test_evidence_stale_when_old(tmp_path: Path) -> None:
    p = tmp_path / "old.json"
    p.write_text("{}", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(p, (old, old))
    status, detail = run_evidence_check(
        {"artifact": "old.json", "predicate": {"type": "exists"}, "max_age_days": 30},
        root=tmp_path,
    )
    assert status is Status.STALE
    assert "old" in detail


def test_evidence_json_all_regex(tmp_path: Path) -> None:
    data = {
        "images": {"a": {"digest": "sha256:" + "a" * 64}, "b": {"digest": "sha256:" + "b" * 64}}
    }
    (tmp_path / "m.json").write_text(json.dumps(data), encoding="utf-8")
    ok, _ = run_evidence_check(
        {
            "artifact": "m.json",
            "predicate": {
                "type": "json_all",
                "path": "images.*.digest",
                "matches": "^sha256:[0-9a-f]{64}$",
            },
        },
        root=tmp_path,
    )
    assert ok is Status.GREEN

    data["images"]["b"]["digest"] = "latest"  # a floating tag sneaks in
    (tmp_path / "m.json").write_text(json.dumps(data), encoding="utf-8")
    bad, _ = run_evidence_check(
        {
            "artifact": "m.json",
            "predicate": {
                "type": "json_all",
                "path": "images.*.digest",
                "matches": "^sha256:[0-9a-f]{64}$",
            },
        },
        root=tmp_path,
    )
    assert bad is Status.MISSING_EVIDENCE


def test_evidence_cyclonedx_components_min(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text(json.dumps({"components": [{"name": "x"}]}), encoding="utf-8")
    ok, _ = run_evidence_check(
        {"artifact": "s.json", "predicate": {"type": "cyclonedx_components_min", "min": 1}},
        root=tmp_path,
    )
    assert ok is Status.GREEN
    (tmp_path / "empty.json").write_text(json.dumps({"components": []}), encoding="utf-8")
    bad, _ = run_evidence_check(
        {"artifact": "empty.json", "predicate": {"type": "cyclonedx_components_min", "min": 1}},
        root=tmp_path,
    )
    assert bad is Status.MISSING_EVIDENCE


def test_evidence_coverage_min_json_and_xml(tmp_path: Path) -> None:
    (tmp_path / "cov.json").write_text(
        json.dumps({"totals": {"percent_covered": 95.0}}), encoding="utf-8"
    )
    ok, _ = run_evidence_check(
        {"artifact": "cov.json", "predicate": {"type": "coverage_min", "min": 93}}, root=tmp_path
    )
    assert ok is Status.GREEN
    (tmp_path / "cov.xml").write_text('<coverage line-rate="0.80"></coverage>', encoding="utf-8")
    low, _ = run_evidence_check(
        {"artifact": "cov.xml", "predicate": {"type": "coverage_min", "min": 93}}, root=tmp_path
    )
    assert low is Status.MISSING_EVIDENCE


def test_evidence_all_of_short_circuits(tmp_path: Path) -> None:
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")
    status, detail = run_evidence_check(
        {
            "all_of": [
                {"artifact": "present.txt", "predicate": {"type": "exists"}},
                {"artifact": "absent.txt", "predicate": {"type": "exists"}},
            ]
        },
        root=tmp_path,
    )
    assert status is Status.MISSING_EVIDENCE
    assert "absent.txt" in detail


# --------------------------- manual attestation ---------------------------- #
def _write_attestation(tmp_path: Path, **fields: object) -> dict:
    body = "\n".join(f"{k}: {v!r}" for k, v in fields.items())
    (tmp_path / "att.yaml").write_text(body, encoding="utf-8")
    return {"attestation": "att.yaml"}


def test_manual_unsigned_is_pending(tmp_path: Path) -> None:
    check = _write_attestation(
        tmp_path, gate="G-PENTEST", signed_off=False, by="", date="", link=""
    )
    status, _ = run_manual_check(check, root=tmp_path)
    assert status is Status.MANUAL_PENDING


def test_manual_missing_file_is_pending(tmp_path: Path) -> None:
    status, detail = run_manual_check({"attestation": "nope.yaml"}, root=tmp_path)
    assert status is Status.MANUAL_PENDING
    assert "no attestation" in detail


def test_manual_signed_off_is_attested(tmp_path: Path) -> None:
    check = _write_attestation(
        tmp_path,
        gate="G-PENTEST",
        signed_off=True,
        by="Ada Lovelace, Acme Security",
        date="2026-07-01",
        link="https://example.com/report",
    )
    status, detail = run_manual_check(check, root=tmp_path)
    assert status is Status.MANUAL_ATTESTED
    assert "Ada Lovelace" in detail


def test_manual_signed_off_but_incomplete_is_pending(tmp_path: Path) -> None:
    check = _write_attestation(
        tmp_path, gate="G-PENTEST", signed_off=True, by="Ada", date="2026-07-01", link=""
    )
    status, _ = run_manual_check(check, root=tmp_path)
    assert status is Status.MANUAL_PENDING  # missing link ⇒ never green


def test_manual_stale_attestation(tmp_path: Path) -> None:
    check = _write_attestation(
        tmp_path,
        gate="G-PENTEST",
        signed_off=True,
        by="Ada",
        date="2020-01-01",
        link="https://example.com",
    )
    check["max_age_days"] = 365
    status, _ = run_manual_check(check, root=tmp_path)
    assert status is Status.STALE
