"""F34 — RuntimeClass render contract (the K8s analogue of daemon.json runtimes).

``helm template`` must render the ``forge-gvisor`` / ``forge-kata-fc``
RuntimeClasses when enabled and omit them when disabled (the default), and the
worker env ConfigMap must reflect ``values.sandbox``.
"""

from __future__ import annotations

import subprocess
from functools import cache

import yaml
from helm_chart_lib import CHART_DIR, require_helm


@cache
def _render(*set_args: str) -> list[dict]:
    helm = require_helm()
    args = [helm, "template", "forge", str(CHART_DIR)]
    for pair in set_args:
        args += ["--set", pair]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed:\n{result.stderr}")
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _runtimeclasses(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "RuntimeClass"}


def _configmap_data(docs: list[dict]) -> dict:
    for d in docs:
        if d.get("kind") == "ConfigMap" and "FORGE_SANDBOX_KIND" in (d.get("data") or {}):
            return d["data"]
    raise AssertionError("env ConfigMap with FORGE_SANDBOX_KIND not rendered")


def test_runtimeclasses_omitted_by_default() -> None:
    """Disabled (default) values render no RuntimeClass objects."""
    assert _runtimeclasses(_render()) == {}


def test_gvisor_runtimeclass_renders_when_enabled() -> None:
    docs = _render("sandbox.runtimeClasses.gvisor.enabled=true")
    classes = _runtimeclasses(docs)
    assert set(classes) == {"forge-gvisor"}
    gvisor = classes["forge-gvisor"]
    assert gvisor["handler"] == "runsc"
    scheduling = gvisor["scheduling"]
    assert scheduling["nodeSelector"] == {"forge.dev/sandbox-runtime": "gvisor"}
    assert scheduling["tolerations"][0]["value"] == "gvisor"


def test_kata_fc_runtimeclass_renders_when_enabled() -> None:
    docs = _render("sandbox.runtimeClasses.kataFc.enabled=true")
    classes = _runtimeclasses(docs)
    assert set(classes) == {"forge-kata-fc"}
    kata = classes["forge-kata-fc"]
    assert kata["handler"] == "kata-fc"
    assert kata["scheduling"]["nodeSelector"] == {"forge.dev/sandbox-runtime": "kata-fc"}


def test_worker_env_reflects_values_sandbox() -> None:
    """The worker's FORGE_SANDBOX_* env is derived from values.sandbox."""
    docs = _render("sandbox.kind=gvisor", "sandbox.gvisorPlatform=kvm")
    data = _configmap_data(docs)
    assert data["FORGE_SANDBOX_KIND"] == "gvisor"
    assert data["FORGE_SANDBOX_GVISOR_RUNTIME"] == "runsc"
    assert data["FORGE_SANDBOX_GVISOR_PLATFORM"] == "kvm"
    assert data["FORGE_SANDBOX_MICROVM_RUNTIME"] == "kata-fc"
    assert data["FORGE_SANDBOX_REQUIRE_KVM"] == "true"
    assert data["FORGE_SANDBOX_JAILER_ROOT"] == "/var/lib/forge/jailer"


def test_default_sandbox_kind_is_worktree() -> None:
    """Opt-in isolation: the chart default stays the V1 worktree kind."""
    data = _configmap_data(_render())
    assert data["FORGE_SANDBOX_KIND"] == "worktree"
