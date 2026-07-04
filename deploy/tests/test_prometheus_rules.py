"""HARD-10 AC15 — Prometheus rules + Alertmanager config are valid as code.

Hermetic (no promtool/amtool required): the recording + alert rule files parse,
every alert declares a runbook annotation, and every metric an expression
references resolves to either the frozen §4 instrument catalog
(``forge_obs.metrics.INSTRUMENT_CATALOG``, allowing the ``_bucket``/``_sum``/
``_count`` histogram suffixes) or a recording rule the same files define. The
Alertmanager config parses and every routed receiver exists. When ``promtool`` /
``amtool`` happen to be on PATH the native validators run too (docker-gated).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from forge_obs.metrics import INSTRUMENT_CATALOG

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
RULES_DIR = DEPLOY / "observability" / "prometheus" / "rules"
RULES_FILE = RULES_DIR / "forge.rules.yml"
ALERTS_FILE = RULES_DIR / "forge.alerts.yml"
ALERTMANAGER_FILE = DEPLOY / "observability" / "alertmanager" / "alertmanager.yml"

#: PromQL aggregation operators + keywords that are not metric selectors.
_PROMQL_KEYWORDS = frozenset(
    {
        "sum",
        "min",
        "max",
        "avg",
        "count",
        "group",
        "stddev",
        "stdvar",
        "topk",
        "bottomk",
        "quantile",
        "count_values",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "offset",
        "bool",
        "and",
        "or",
        "unless",
        "le",
        "inf",
        "nan",
    }
)

#: Prometheus builtin series that are always available.
_BUILTIN_METRICS = frozenset({"up"})

#: Histogram/summary suffixes Prometheus derives from a base instrument.
_SUFFIXES = ("_bucket", "_sum", "_count")

_IDENT_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_STRING_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_GROUPING_RE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
_MATCHER_RE = re.compile(r"\{[^}]*\}")
_DURATION_RE = re.compile(r"\[[^\]]*\]")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _defined_record_names() -> set[str]:
    names: set[str] = set()
    for path in (RULES_FILE, ALERTS_FILE):
        for group in _load(path).get("groups", []):
            for rule in group.get("rules", []):
                if "record" in rule:
                    names.add(rule["record"])
    return names


def _all_exprs() -> list[tuple[str, str]]:
    """(source, expr) pairs across both rule files."""
    out: list[tuple[str, str]] = []
    for path in (RULES_FILE, ALERTS_FILE):
        for group in _load(path).get("groups", []):
            for rule in group.get("rules", []):
                label = rule.get("record") or rule.get("alert") or "?"
                out.append((f"{path.name}:{label}", rule["expr"]))
    return out


def _metric_candidates(expr: str) -> set[str]:
    cleaned = _STRING_RE.sub(" ", expr)
    cleaned = _GROUPING_RE.sub(" ", cleaned)
    cleaned = _MATCHER_RE.sub(" ", cleaned)
    cleaned = _DURATION_RE.sub(" ", cleaned)
    candidates: set[str] = set()
    for m in _IDENT_RE.finditer(cleaned):
        token = m.group(0)
        nxt = cleaned[m.end() : m.end() + 1]
        if nxt == "(":  # a function call, not a selector
            continue
        if token in _PROMQL_KEYWORDS:
            continue
        candidates.add(token)
    return candidates


def _resolves(token: str, records: set[str]) -> bool:
    if ":" in token:  # a recording-rule series
        return token in records
    if token in _BUILTIN_METRICS or token in INSTRUMENT_CATALOG:
        return True
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)] in INSTRUMENT_CATALOG:
            return True
    return False


def test_rule_files_parse() -> None:
    for path in (RULES_FILE, ALERTS_FILE):
        doc = _load(path)
        assert doc.get("groups"), f"{path.name} has no rule groups"


def test_every_expr_references_known_metrics() -> None:
    records = _defined_record_names()
    unknown: list[str] = []
    for source, expr in _all_exprs():
        for token in _metric_candidates(expr):
            if not _resolves(token, records):
                unknown.append(f"{source}: {token!r}")
    assert not unknown, "expressions reference unknown metrics/series:\n" + "\n".join(unknown)


def test_every_alert_has_runbook_and_severity() -> None:
    doc = _load(ALERTS_FILE)
    for group in doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" not in rule:
                continue
            name = rule["alert"]
            assert rule.get("labels", {}).get("severity"), f"{name} missing severity label"
            assert rule.get("annotations", {}).get("runbook"), f"{name} missing runbook annotation"
            assert rule.get("annotations", {}).get("summary"), f"{name} missing summary"


def test_expected_alerts_present() -> None:
    names = {
        rule["alert"]
        for group in _load(ALERTS_FILE)["groups"]
        for rule in group.get("rules", [])
        if "alert" in rule
    }
    for expected in ("ForgeOtlpExporterDown", "ForgeCostEmitFailures", "ForgeAgentFailureRateHigh"):
        assert expected in names, f"missing expected alert {expected}"


def test_alertmanager_config_valid() -> None:
    doc = _load(ALERTMANAGER_FILE)
    route = doc.get("route")
    receivers = {r["name"] for r in doc.get("receivers", [])}
    assert route and route.get("receiver") in receivers, "root route receiver undefined"
    assert receivers, "no receivers defined"
    for sub in route.get("routes", []):
        assert sub["receiver"] in receivers, f"sub-route receiver {sub['receiver']} undefined"
    for r in doc["receivers"]:
        assert r.get("webhook_configs"), f"receiver {r['name']} has no delivery config"


@pytest.mark.docker
def test_promtool_check_rules() -> None:
    if shutil.which("promtool") is None:
        pytest.skip("promtool not on PATH")
    for path in (RULES_FILE, ALERTS_FILE):
        proc = subprocess.run(
            ["promtool", "check", "rules", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.docker
def test_amtool_check_config() -> None:
    if shutil.which("amtool") is None:
        pytest.skip("amtool not on PATH")
    proc = subprocess.run(
        ["amtool", "check-config", str(ALERTMANAGER_FILE)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
