"""#293: the chart gives the api, the Celery worker and a Temporal worker one durable engine.

The chart always runs ``FORGE_ENV=production``, where a repository-changing run
without the durable engine is refused (#277, spec FR-005). So the three workloads
must see the same ``WORKFLOW_ENGINE_BACKEND`` and ``TEMPORAL_*`` settings, and the
codec key and the master key must reach them from a Secret, never a ConfigMap.
"""

from __future__ import annotations

import subprocess
from functools import cache

import yaml
from helm_chart_lib import CHART_DIR, render_docs, require_helm

EXTERNAL_HOST = "temporal-frontend.example.com:7233"
CODEC_KEY = "chart-test-codec-key"
ENABLED = (
    "temporal.enabled=true",
    f"temporal.host={EXTERNAL_HOST}",
    f"secrets.data.TEMPORAL_CODEC_KEY={CODEC_KEY}",
)
ENGINE_KEYS = (
    "WORKFLOW_ENGINE_BACKEND",
    "TEMPORAL_HOST",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TASK_QUEUE",
    "TEMPORAL_CODEC_KEY",
)
# SECRET_KEY is the master key the chart's Secret carries (the API reads it as the
# FORGE_SECRET_KEY alias).
SECRET_KEYS = ("TEMPORAL_CODEC_KEY", "SECRET_KEY")
ENGINE_WORKLOADS = ("forge-api", "forge-worker", "forge-temporal-worker")


def _helm(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = [require_helm(), "template", "forge", str(CHART_DIR)]
    for pair in set_args:
        args += ["--set", pair]
    return subprocess.run(args, capture_output=True, text=True, check=False)


@cache
def _render(*set_args: str) -> list[dict]:
    result = _helm(*set_args)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed:\n{result.stderr}")
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _deployments(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}


def _container(deploy: dict) -> dict:
    return deploy["spec"]["template"]["spec"]["containers"][0]


def _env(docs: list[dict], deploy: dict) -> dict[str, tuple[str, str]]:
    """Every variable the container sees -> (kind of its source, value), as Kubernetes
    resolves it: envFrom in order, later sources winning, then explicit env."""
    data = {
        (d["kind"], d["metadata"]["name"]): {**(d.get("data") or {}), **(d.get("stringData") or {})}
        for d in docs
        if d.get("kind") in {"ConfigMap", "Secret"}
    }
    env: dict[str, tuple[str, str]] = {}
    for ref in _container(deploy).get("envFrom", []):
        kind = "ConfigMap" if "configMapRef" in ref else "Secret"
        name = next(iter(ref.values()))["name"]
        for key, value in data[(kind, name)].items():
            env[key] = (kind, value)
    for var in _container(deploy).get("env", []):
        env[var["name"]] = ("env", var.get("value"))
    return env


def test_engine_is_off_by_default_and_renders_no_temporal_worker() -> None:
    docs = render_docs("default")
    assert "forge-temporal-worker" not in _deployments(docs)
    for name in ("forge-api", "forge-worker"):
        env = _env(docs, _deployments(docs)[name])
        assert env["WORKFLOW_ENGINE_BACKEND"] == ("ConfigMap", "postgres_fsm")


def test_three_workloads_see_the_same_engine_settings_and_master_key() -> None:
    docs = _render(*ENABLED)
    deployments = _deployments(docs)
    envs = {name: _env(docs, deployments[name]) for name in ENGINE_WORKLOADS}
    api = envs["forge-api"]
    assert api["WORKFLOW_ENGINE_BACKEND"] == ("ConfigMap", "temporal")
    assert api["TEMPORAL_HOST"] == ("ConfigMap", EXTERNAL_HOST)
    assert api["TEMPORAL_NAMESPACE"] == ("ConfigMap", "forge")
    assert api["TEMPORAL_TASK_QUEUE"] == ("ConfigMap", "forge-feature")
    assert api["TEMPORAL_CODEC_KEY"] == ("Secret", CODEC_KEY)
    for name, env in envs.items():
        for key in (*ENGINE_KEYS, "SECRET_KEY"):
            assert env.get(key) == api[key], f"{name} sees a different {key}"


def test_codec_key_and_master_key_come_from_a_secret_never_a_configmap() -> None:
    for docs in (_render(*ENABLED), render_docs("production")):
        for cm in (d for d in docs if d.get("kind") == "ConfigMap"):
            leaked = set(SECRET_KEYS) & set(cm.get("data") or {})
            assert not leaked, f"ConfigMap/{cm['metadata']['name']} carries {leaked}"
    docs = _render(*ENABLED)
    for name in ENGINE_WORKLOADS:
        env = _env(docs, _deployments(docs)[name])
        for key in SECRET_KEYS:
            assert env[key][0] == "Secret", f"{name} {key} comes from {env[key][0]}"


def test_production_profile_runs_the_engine_from_the_operator_secret() -> None:
    """The production profile reads every secret from secrets.existingSecret, so the
    codec key and the master key reach all three workloads through that one Secret."""
    docs = render_docs("production")
    deployments = _deployments(docs)
    sources = [_container(deployments[name])["envFrom"] for name in ENGINE_WORKLOADS]
    assert sources[0] == sources[1] == sources[2]
    assert {"secretRef": {"name": "forge-external"}} in sources[0]
    for name in ENGINE_WORKLOADS:
        assert not _container(deployments[name]).get("env"), f"{name} overrides envFrom"
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")["data"]
    assert cm["WORKFLOW_ENGINE_BACKEND"] == "temporal"
    assert cm["TEMPORAL_HOST"]


def test_temporal_worker_runs_the_temporal_entrypoint_on_the_worker_image() -> None:
    deployments = _deployments(_render(*ENABLED))
    worker = _container(deployments["forge-temporal-worker"])
    assert worker["command"] == ["python", "-m", "scf_worker.temporal_main"]
    assert worker["image"] == _container(deployments["forge-worker"])["image"]


def test_temporal_worker_reaches_the_datastores_gateway_and_api() -> None:
    """Under NetworkPolicy the Temporal worker gets the Celery worker's lanes."""
    policies = {
        d["metadata"]["name"]: d for d in _render(*ENABLED) if d.get("kind") == "NetworkPolicy"
    }
    for name in ("forge-datastores", "forge-mcp-gateway", "forge-api"):
        components = [
            expr["values"]
            for rule in policies[name]["spec"]["ingress"]
            for peer in rule.get("from", [])
            for expr in (peer.get("podSelector") or {}).get("matchExpressions", [])
            if expr["key"] == "app.kubernetes.io/component"
        ]
        assert any("temporal-worker" in values for values in components), name


def test_enabling_the_engine_without_an_external_host_fails() -> None:
    result = _helm("temporal.enabled=true", f"secrets.data.TEMPORAL_CODEC_KEY={CODEC_KEY}")
    assert result.returncode != 0
    assert "temporal.host" in result.stderr


def test_enabling_the_engine_without_a_codec_key_fails() -> None:
    result = _helm("temporal.enabled=true", f"temporal.host={EXTERNAL_HOST}")
    assert result.returncode != 0
    assert "TEMPORAL_CODEC_KEY" in result.stderr
