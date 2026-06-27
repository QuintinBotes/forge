"""Settings env-binding (FORGE_SANDBOX_*)."""

from __future__ import annotations

from forge_agent.sandbox import SandboxSettings
from forge_contracts import SandboxKind, SandboxNetwork


def test_defaults() -> None:
    s = SandboxSettings()
    assert s.kind is SandboxKind.WORKTREE
    assert s.docker_host == "tcp://docker-proxy:2375"
    assert s.network is SandboxNetwork.NONE
    assert s.output_cap_bytes == 262144
    assert s.run_uid == 10001
    assert s.resolved_allowed_images() == (s.image_python, s.image_node, s.image_go)


def test_from_env_overrides() -> None:
    env = {
        "FORGE_SANDBOX_KIND": "container",
        "FORGE_SANDBOX_DOCKER_HOST": "tcp://docker-proxy:2375",
        "FORGE_SANDBOX_MEMORY_MB": "8192",
        "FORGE_SANDBOX_CPUS": "4.0",
        "FORGE_SANDBOX_NETWORK": "egress",
        "FORGE_SANDBOX_ALLOWED_IMAGES": "a:1,b:2",
        "FORGE_SANDBOX_EGRESS_ALLOWLIST": "pypi.org,files.pythonhosted.org",
        "FORGE_SANDBOX_OUTPUT_CAP_BYTES": "1024",
        "FORGE_SANDBOX_MAX_TTL_SECONDS": "60",
    }
    s = SandboxSettings.from_env(env)
    assert s.kind is SandboxKind.CONTAINER
    assert s.memory_mb == 8192
    assert s.cpus == 4.0
    assert s.network is SandboxNetwork.EGRESS
    assert s.resolved_allowed_images() == ("a:1", "b:2")
    assert s.egress_allowlist == ("pypi.org", "files.pythonhosted.org")
    assert s.output_cap_bytes == 1024
    assert s.max_ttl_seconds == 60


def test_image_for_language() -> None:
    s = SandboxSettings()
    assert s.image_for("typescript") == s.image_node
    assert s.image_for("golang") == s.image_go
    assert s.image_for(None) == s.image_python
