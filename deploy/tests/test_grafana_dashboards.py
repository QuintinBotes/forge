"""HARD-10 AC15 — Grafana dashboards + provisioning are valid as code.

Every ``deploy/observability/grafana/dashboards/*.json`` parses, declares a
unique ``uid`` + ``title``, and every panel target references only a datasource
``uid`` that the provisioning file declares. The four spec dashboards
(Workflow/Agent/Retrieval/Cost) are all present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
DASH_DIR = DEPLOY / "observability" / "grafana" / "dashboards"
DS_FILE = DEPLOY / "observability" / "grafana" / "provisioning" / "datasources" / "datasources.yaml"

REQUIRED_DASHBOARDS = {
    "workflow-quality.json",
    "agent-quality.json",
    "retrieval-quality.json",
    "cost.json",
}


def _provisioned_uids() -> set[str]:
    doc = yaml.safe_load(DS_FILE.read_text(encoding="utf-8"))
    return {ds["uid"] for ds in doc["datasources"]}


def _dashboards() -> list[Path]:
    return sorted(DASH_DIR.glob("*.json"))


def _iter_datasource_refs(node) -> list[str]:
    """Walk a dashboard dict collecting every datasource uid referenced."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "datasource" and isinstance(value, dict) and "uid" in value:
                found.append(value["uid"])
            else:
                found.extend(_iter_datasource_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_datasource_refs(item))
    return found


def test_all_required_dashboards_present() -> None:
    names = {p.name for p in _dashboards()}
    assert names >= REQUIRED_DASHBOARDS, f"missing dashboards: {REQUIRED_DASHBOARDS - names}"


def test_dashboards_parse_and_have_unique_uid_and_title() -> None:
    uids: dict[str, str] = {}
    titles: dict[str, str] = {}
    for path in _dashboards():
        dash = json.loads(path.read_text(encoding="utf-8"))
        uid, title = dash.get("uid"), dash.get("title")
        assert uid, f"{path.name} missing uid"
        assert title, f"{path.name} missing title"
        assert uid not in uids, f"duplicate uid {uid} in {path.name} and {uids[uid]}"
        assert title not in titles, f"duplicate title {title!r}"
        uids[uid] = path.name
        titles[title] = path.name


def test_dashboards_reference_only_provisioned_datasources() -> None:
    provisioned = _provisioned_uids()
    for path in _dashboards():
        dash = json.loads(path.read_text(encoding="utf-8"))
        for uid in _iter_datasource_refs(dash):
            assert uid in provisioned, (
                f"{path.name} references unprovisioned datasource uid {uid!r} "
                f"(provisioned: {sorted(provisioned)})"
            )


def test_dashboards_have_panels() -> None:
    for path in _dashboards():
        dash = json.loads(path.read_text(encoding="utf-8"))
        assert dash.get("panels"), f"{path.name} has no panels"
