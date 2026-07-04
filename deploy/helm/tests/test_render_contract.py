"""Render-contract tests over ``helm template`` output (AC2, AC4, AC11, AC15, AC17).

One place that enforces the "every workload" invariants from the slice §4.2 so
they stay true as workloads are added, plus a kubeconform schema-conformance pass.
Skips (parked) when ``helm`` / ``kubeconform`` are not installed.
"""

from __future__ import annotations

import re
import subprocess

import pytest
import yaml
from helm_chart_lib import KUBECONFORM, PROFILES, WORKLOADS, render_docs, require_helm

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
RECOMMENDED_LABELS = [
    "app.kubernetes.io/name",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/version",
    "app.kubernetes.io/component",
    "app.kubernetes.io/part-of",
    "app.kubernetes.io/managed-by",
    "helm.sh/chart",
]


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _named(docs: list[dict], kind: str, name: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"{kind}/{name} not found in rendered output")


def _containers(deploy: dict) -> list[dict]:
    return deploy["spec"]["template"]["spec"]["containers"]


@pytest.mark.parametrize("profile", list(PROFILES), ids=list(PROFILES))
def test_profile_renders(profile: str) -> None:
    """AC2 — every profile renders parseable multi-doc YAML."""
    require_helm()
    assert render_docs(profile), f"{profile} rendered no documents"


def test_every_object_has_recommended_labels() -> None:
    """AC17 — recommended labels on every object the chart owns."""
    require_helm()
    for doc in render_docs("default"):
        labels = (doc.get("metadata") or {}).get("labels") or {}
        # Skip subchart (bundled datastore) objects — they carry their own labels.
        if labels.get("app.kubernetes.io/part-of") != "forge":
            continue
        for key in RECOMMENDED_LABELS:
            assert key in labels, f"{doc['kind']}/{doc['metadata']['name']} missing {key}"


@pytest.mark.parametrize("name", WORKLOADS)
def test_workload_is_hardened(name: str) -> None:
    """§4.2 — every workload renders the hardened securityContext + resources."""
    require_helm()
    deploy = _named(render_docs("default"), "Deployment", name)
    pod = deploy["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    sc = _containers(deploy)[0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert "ALL" in sc["capabilities"]["drop"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    resources = _containers(deploy)[0]["resources"]
    for kind in ("requests", "limits"):
        assert resources[kind]["cpu"]
        assert resources[kind]["memory"]


@pytest.mark.parametrize("name", WORKLOADS)
def test_workload_consumes_config_and_secret(name: str) -> None:
    """§4.2 — each pod reads env from the ConfigMap and the Secret via envFrom."""
    require_helm()
    deploy = _named(render_docs("default"), "Deployment", name)
    refs = {next(iter(e)) for e in _containers(deploy)[0]["envFrom"]}
    assert {"configMapRef", "secretRef"} <= refs


def test_secret_and_config_keys_are_disjoint() -> None:
    """AC9 — no key in the Secret also appears in the ConfigMap."""
    require_helm()
    docs = render_docs("default")
    cm_keys = set(_named(docs, "ConfigMap", "forge-env").get("data") or {})
    secret = _named(docs, "Secret", "forge-secret")
    sec_keys = set(secret.get("stringData") or {}) | set(secret.get("data") or {})
    assert cm_keys & sec_keys == set(), f"leaked keys: {cm_keys & sec_keys}"
    boot = {
        "SECRET_KEY",
        "AUTH_SECRET",
        "FORGE_VAULT_KEYS",
        "API_KEY_PEPPER",
        "INTERNAL_SERVICE_TOKEN",
        "MODEL_PROVIDER_KEY",
    }
    assert boot <= sec_keys
    assert "FORGE_VAULT_ACTIVE_KEY_VERSION" in cm_keys


def test_production_images_are_digest_pinned() -> None:
    """AC4 — under the production profile every forge image is @sha256-pinned."""
    require_helm()
    docs = render_docs("production")
    forge_images = [
        c["image"]
        for d in docs
        if d.get("kind") in {"Deployment", "Job"}
        for c in _containers(d)
        if "ghcr.io/forge-platform" in c["image"]
    ]
    assert forge_images, "no forge workload images found in production render"
    for image in forge_images:
        assert DIGEST_RE.search(image), f"image not digest-pinned: {image}"


def test_production_emits_no_bundled_stateful_objects() -> None:
    """AC11 — external/managed mode renders no bundled StatefulSet/PVC."""
    require_helm()
    docs = render_docs("production")
    assert _by_kind(docs, "StatefulSet") == []
    assert _by_kind(docs, "PersistentVolumeClaim") == []
    cm = _named(docs, "ConfigMap", "forge-env")["data"]
    assert cm["POSTGRES_HOST"] == "forge-postgres.managed.example.com"


@pytest.mark.parametrize("profile", list(PROFILES), ids=list(PROFILES))
def test_kubeconform_conformance(profile: str) -> None:
    """AC15 — every rendered resource is schema-valid for Kubernetes 1.28."""
    require_helm()
    if KUBECONFORM is None:
        pytest.skip("PARKED: `kubeconform` not on PATH — conformance runs in CI.")
    manifest = "\n---\n".join(yaml.safe_dump(d) for d in render_docs(profile))
    result = subprocess.run(
        [
            KUBECONFORM,
            "-strict",
            "-ignore-missing-schemas",
            "-kubernetes-version",
            "1.28.0",
            "-schema-location",
            "default",
            "-summary",
        ],
        input=manifest,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"kubeconform ({profile}) failed:\n{result.stdout}\n{result.stderr}"
    )
