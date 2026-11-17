"""The MCP conformance runner pins its suite and justifies its baseline (spec 007 FR-007)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from scf_api.mcp_server.protocol import CURRENT_VERSION

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mcp-conformance.sh"
BASELINE = ROOT / "scripts" / "mcp-conformance-baseline.yaml"
PACKAGE = "@modelcontextprotocol/conformance"
#: An exact npm version: no range, tag or wildcard.
EXACT = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?")


def _assignment(text: str, name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"$', text, re.MULTILINE)
    assert match, f"{SCRIPT.name} must assign {name}"
    return match.group(1)


def test_script_pins_one_exact_suite_version_and_the_server_revision() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert os.access(SCRIPT, os.X_OK)
    assert EXACT.fullmatch(_assignment(text, "SUITE_VERSION"))
    assert _assignment(text, "REVISION") == CURRENT_VERSION
    references = re.findall(re.escape(PACKAGE) + r"(\S*)", text)
    assert references, "the script must name the suite package"
    assert all(ref.startswith("@${SUITE_VERSION}") for ref in references), references
    assert "latest" not in text
    assert '--requirements "${REVISION}"' in text
    assert '--expected-failures "${BASELINE}"' in text


def test_the_suite_runs_without_the_api_key_in_its_environment() -> None:
    """Only the loopback proxy needs the key; npx and its install scripts must not see it."""
    lines = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()]
    launches = [line for line in lines if re.search(r"\bnpx\b", line) and line[:1] != "#"]
    assert launches, "the script must launch the suite with npx"
    assert all(line.startswith("env -u SCF_MCP_TOKEN npx ") for line in launches), launches


def test_every_baseline_entry_has_a_written_reason() -> None:
    text = BASELINE.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    assert set(loaded) == {"server"}
    entries = loaded["server"]
    assert entries and all(isinstance(entry, str) for entry in entries)
    reasons = {}
    for line in text.splitlines():
        match = re.match(r"\s*-\s+(\S+)\s*(?:#(.*))?$", line)
        if match:
            reasons[match.group(1)] = (match.group(2) or "").strip()
    assert sorted(reasons) == sorted(entries)
    missing = [entry for entry, reason in reasons.items() if not reason]
    assert not missing, f"baseline entries without a reason: {missing}"
