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
from helm_chart_lib import (
    CHART_DIR,
    ENGINE_CODEC_KEY,
    ENGINE_HOST,
    render_docs,
    require_helm,
)

EXTERNAL_HOST = ENGINE_HOST
CODEC_KEY = ENGINE_CODEC_KEY
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
        ref = (var.get("valueFrom") or {}).get("secretKeyRef")
        if ref:
            env[var["name"]] = ("Secret", data.get(("Secret", ref["name"]), {}).get(ref["key"]))
        else:
            env[var["name"]] = ("env", var.get("value"))
    return env


def _codec_key_ref(secret: str) -> dict:
    return {
        "name": "TEMPORAL_CODEC_KEY",
        "valueFrom": {
            "secretKeyRef": {"name": secret, "key": "TEMPORAL_CODEC_KEY", "optional": False}
        },
    }


def test_engine_is_off_by_default_and_renders_no_temporal_worker() -> None:
    docs = render_docs("default")
    assert "forge-temporal-worker" not in _deployments(docs)
    for name in ("forge-api", "forge-worker"):
        env = _env(docs, _deployments(docs)[name])
        assert env["WORKFLOW_ENGINE_BACKEND"] == ("ConfigMap", "postgres_fsm")


def test_three_workloads_see_the_same_engine_settings_and_master_key() -> None:
    docs = render_docs("engine")
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
    for docs in (render_docs("engine"), render_docs("production")):
        for cm in (d for d in docs if d.get("kind") == "ConfigMap"):
            leaked = set(SECRET_KEYS) & set(cm.get("data") or {})
            assert not leaked, f"ConfigMap/{cm['metadata']['name']} carries {leaked}"
    docs = render_docs("engine")
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
        # Only the required codec-key ref (from that same Secret) sits beside envFrom.
        env = _container(deployments[name]).get("env")
        assert env == [_codec_key_ref("forge-external")], f"{name} env: {env}"
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")["data"]
    assert cm["WORKFLOW_ENGINE_BACKEND"] == "temporal"
    assert cm["TEMPORAL_HOST"]


def test_engine_workloads_refuse_to_start_without_the_codec_key() -> None:
    """The chart cannot read an existing Secret, so each engine container requires the
    key by secretKeyRef: a Secret without it stops the pod from starting, instead of
    the process running without the codec (unencrypted, unredacted payloads)."""
    for profile, secret in (("engine", "forge-secret"), ("production", "forge-external")):
        deployments = _deployments(render_docs(profile))
        for name in ENGINE_WORKLOADS:
            env = _container(deployments[name]).get("env") or []
            assert _codec_key_ref(secret) in env, f"{profile} {name} env: {env}"
    deployments = _deployments(render_docs("default"))
    for name in ("forge-api", "forge-worker"):
        assert not _container(deployments[name]).get("env"), name


def test_temporal_worker_runs_the_temporal_entrypoint_on_the_worker_image() -> None:
    deployments = _deployments(render_docs("engine"))
    worker = _container(deployments["forge-temporal-worker"])
    assert worker["command"] == ["python", "-m", "scf_worker.temporal_main"]
    assert worker["image"] == _container(deployments["forge-worker"])["image"]


def test_temporal_worker_reaches_the_datastores_gateway_and_api() -> None:
    """Under NetworkPolicy the Temporal worker gets the Celery worker's lanes."""
    policies = {
        d["metadata"]["name"]: d for d in render_docs("engine") if d.get("kind") == "NetworkPolicy"
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


def test_workers_keep_repository_mirrors_on_a_writable_mount() -> None:
    """The root filesystem is read-only, so the mirror root (the app default when
    unset) must sit under a writable volume in every workload that clones."""
    docs = render_docs("engine")
    for name in ("forge-worker", "forge-temporal-worker"):
        deploy = _deployments(docs)[name]
        mirrors = _env(docs, deploy).get("FORGE_REPO_MIRRORS_DIR", ("", "/var/lib/forge/mirrors"))
        writable = [
            m["mountPath"] for m in _container(deploy)["volumeMounts"] if not m.get("readOnly")
        ]
        assert any(mirrors[1].startswith(f"{path}/") for path in writable), (
            f"{name} clones into {mirrors[1]}, not under a writable mount {writable}"
        )


def test_production_runs_one_temporal_worker() -> None:
    """A run's unpushed branch lives in one pod's mirror, and its activities can land on
    any replica, so production runs one until mirrors are shared storage (#237)."""
    deploy = _deployments(render_docs("production"))["forge-temporal-worker"]
    assert deploy["spec"]["replicas"] == 1


def _checksums(docs: list[dict]) -> dict[str, dict[str, str]]:
    deployments = _deployments(docs)
    return {
        name: {
            key: value
            for key, value in (
                deployments[name]["spec"]["template"]["metadata"].get("annotations") or {}
            ).items()
            if key.startswith("checksum/")
        }
        for name in ENGINE_WORKLOADS
    }


def test_engine_workloads_roll_together_when_the_config_changes() -> None:
    """envFrom is read at container start, so a changed engine setting must change the
    pod template of all three, or `helm upgrade` leaves them on the old engine."""
    on = _checksums(render_docs("engine"))
    assert set(on["forge-api"]) == {"checksum/config", "checksum/secret"}
    assert on["forge-api"] == on["forge-worker"] == on["forge-temporal-worker"]
    moved = _checksums(_render(*ENABLED, "temporal.taskQueue=another-queue"))
    assert moved["forge-api"]["checksum/config"] != on["forge-api"]["checksum/config"]
    assert moved["forge-api"] == moved["forge-worker"] == moved["forge-temporal-worker"]
    # An operator-managed Secret is not rendered, so only the config is summed.
    production = _checksums(render_docs("production"))
    assert set(production["forge-api"]) == {"checksum/config"}
    assert production["forge-api"] == production["forge-temporal-worker"]
