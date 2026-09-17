"""Compose contract: durable persistence and a pullable object store.

Two structural guarantees, both from the 2026-09-17 self-host evaluation:

* **Persistence selection.** Every ``FORGE_*_BACKEND`` seam defaults to
  ``memory`` in ``forge_api.settings`` so the unit suite stays Postgres-free.
  That default is a *test* default. A deployment that leaves it unset keeps
  minted API keys, board rows, audit entries, approvals and BYOK ciphertext in
  the API process and loses them on restart — which is how an evaluator lost an
  afternoon minting keys that never reached the database. The compose files are
  the composition root; these tests hold them to selecting ``db``.

* **A pullable MinIO.** ``docker.io/minio/minio`` is no longer anonymously
  pullable (the whole repository 401s, not just the pinned digest), so a stack
  pinned there cannot start at all. quay.io is MinIO's public registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
PROD = DEPLOY / "docker-compose.yml"
DEV = DEPLOY / "docker-compose.dev.yml"

#: Every persistence seam with a Postgres-backed implementation behind it.
PERSISTENCE_VARS = (
    "FORGE_APIKEY_BACKEND",
    "FORGE_APPROVAL_BACKEND",
    "FORGE_AUDIT_BACKEND",
    "FORGE_BOARD_BACKEND",
    "FORGE_OVERRIDE_GRANT_BACKEND",
    "FORGE_PM_LINK_BACKEND",
    "FORGE_POLICY_AUDIT_BACKEND",
    "FORGE_PROJECTION_BACKEND",
    "FORGE_SECRET_BACKEND",
)

#: Services running first-party Python that resolve these settings.
PROD_PYTHON_SERVICES = ("api", "worker", "mcp-gateway", "temporal-worker")
DEV_PYTHON_SERVICES = ("api", "worker", "mcp-gateway", "migrate", "seed")


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env(path: Path, service: str) -> dict:
    env = _compose(path)["services"][service].get("environment") or {}
    assert isinstance(env, dict), f"{service} uses list-form environment"
    return env


# --- persistence selection -------------------------------------------------- #


@pytest.mark.parametrize("service", PROD_PYTHON_SERVICES)
@pytest.mark.parametrize("var", PERSISTENCE_VARS)
def test_production_selects_durable_backends(service: str, var: str) -> None:
    value = _env(PROD, service).get(var)
    assert value is not None, f"{service} does not receive {var}"
    assert value.endswith(":-db}"), f"{service} {var} does not default to db: {value!r}"


@pytest.mark.parametrize("service", DEV_PYTHON_SERVICES)
@pytest.mark.parametrize("var", PERSISTENCE_VARS)
def test_dev_selects_durable_backends(service: str, var: str) -> None:
    value = _env(DEV, service).get(var)
    assert value is not None, f"{service} does not receive {var}"
    assert value.endswith(":-db}"), f"{service} {var} does not default to db: {value!r}"


@pytest.mark.parametrize("var", PERSISTENCE_VARS)
def test_every_backend_var_stays_overridable(var: str) -> None:
    """A deliberately ephemeral stack must still be one env var away."""
    for path in (PROD, DEV):
        value = _env(path, "api")[var]
        assert value.startswith("${"), f"{path.name} hardcodes {var}"


def test_the_documented_defaults_match_the_compose_files() -> None:
    """`.env.example` is what an operator copies — it must not contradict them."""
    env_example = (DEPLOY.parent / ".env.example").read_text(encoding="utf-8")
    for var in PERSISTENCE_VARS:
        assert f"{var}=db" in env_example, f".env.example does not document {var}"


# --- object store ----------------------------------------------------------- #


@pytest.mark.parametrize("path", [PROD, DEV], ids=["production", "dev"])
def test_minio_is_pinned_to_the_public_registry(path: Path) -> None:
    image = _compose(path)["services"]["minio"]["image"]
    assert image.startswith("quay.io/minio/minio:"), (
        "docker.io/minio/minio is not anonymously pullable — the repository "
        f"itself 401s, so this stack cannot start. Got: {image}"
    )
    assert "@sha256:" in image, "pulled images stay digest-pinned (HARD-07)"


def test_the_build_manifest_records_the_same_minio() -> None:
    import json

    manifest = json.loads((DEPLOY / "build-manifest.json").read_text(encoding="utf-8"))
    image = _compose(PROD)["services"]["minio"]["image"]
    bare, _, digest = image.partition("@")
    assert manifest["images"][bare]["digest"] == digest


# --- the edge is the single origin ------------------------------------------ #


def test_the_dev_web_reaches_the_api_through_caddy() -> None:
    """Pointing the browser at :8000 directly is what produced a CORS-Offline UI."""
    env = _env(DEV, "web")
    assert "CADDY_PORT" in env["NEXT_PUBLIC_API_URL"]
    assert env["NEXT_PUBLIC_API_URL"].rstrip("}").endswith("/api")


@pytest.mark.parametrize("caddyfile", ["Caddyfile", "Caddyfile.dev"], ids=["production", "dev"])
def test_the_edge_proxies_the_realtime_sockets(caddyfile: str) -> None:
    """Both sockets: the board channel at /ws, spec collab under /ws/spec/{id}."""
    text = (DEPLOY / "caddy" / caddyfile).read_text(encoding="utf-8")
    assert "@realtime path /ws /ws/*" in text
    assert "handle @realtime" in text


# --- the spec engine's store is durable and shared (issue #110) ------------ #

#: Services that resolve ``FORGE_SPEC_ROOT`` and must see the same documents.
SPEC_ENGINE_SERVICES = ("api", "worker", "mcp-gateway")


@pytest.mark.parametrize("path", [PROD, DEV], ids=["production", "dev"])
@pytest.mark.parametrize("service", SPEC_ENGINE_SERVICES)
def test_the_spec_store_is_on_a_named_volume(path: Path, service: str) -> None:
    """The spec engine is filesystem-backed, so the filesystem has to survive.

    `spec_root` defaults to the relative path `specs`, which lands on the
    container's own writable layer: every spec is destroyed by a rebuild, the
    key allocator restarts at 1 and hands back a key already in use, and the
    worker cannot see a spec the API wrote.
    """
    svc = _compose(path)["services"][service]
    env = svc.get("environment") or {}
    assert env.get("FORGE_SPEC_ROOT") == "/srv/forge/specs", (
        f"{service} does not point the spec engine at the shared volume"
    )

    mounts = [v if isinstance(v, str) else v.get("source", "") for v in (svc.get("volumes") or [])]
    assert any("forge-specs:/srv/forge/specs" in m for m in mounts), (
        f"{service} does not mount the spec volume, so its specs are ephemeral"
    )


@pytest.mark.parametrize("path", [PROD, DEV], ids=["production", "dev"])
def test_the_spec_volume_is_declared(path: Path) -> None:
    assert "forge-specs" in (_compose(path).get("volumes") or {})


@pytest.mark.parametrize("dockerfile", ["api", "worker", "mcp-gateway"])
def test_the_image_seeds_the_spec_dir_owned_by_the_runtime_user(dockerfile: str) -> None:
    """A volume mounted onto a path absent from the image is created root-owned.

    These images run as `forge` (1000:1000), so the directory has to exist and
    be owned before the mount, or the engine cannot write a single spec.
    """
    text = (DEPLOY / "docker" / f"{dockerfile}.Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p /srv/forge/specs" in text
    assert "chown -R forge:forge /srv/forge" in text
    # The chown must precede `USER forge`, or it cannot chown at all.
    assert text.index("chown -R forge:forge /srv/forge") < text.index("USER forge")
