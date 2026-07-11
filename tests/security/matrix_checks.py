"""Offline enforcement-matrix checks (HARD-09).

Each function here is named by a row in ``security/enforcement-matrix.yaml``
(`check:` key) and asserts one FORGE_SPEC security control **on the wired
path** — route -> dependency -> primitive — so a control that is implemented
but never mounted is still a red test. Pure asserts, no fixtures, no network,
no DB, no external creds (live-db rows live in ``test_enforcement_matrix.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.settings import Settings
from forge_contracts import UserRole

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-00000000c0de")
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000beef")

# Secret-SHAPED sample values (the redactor is deliberately conservative: it
# strips secret-named keys and secret-shaped substrings, so the samples below
# use canonical shapes — a provider `sk-` key, a JWT, an AWS AKIA key, and a
# Bearer token — exactly what the spec's redaction control must catch).
_SK_KEY = "sk-abcdefghijklmnop0123456789ABCDEF"
_BEARER_TOKEN = "AbCdEf0123456789AbCdEf0123456789ghij"
_BEARER = f"Bearer {_BEARER_TOKEN}"
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _principal(role: UserRole) -> Principal:
    return Principal(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        role=role,
        email="matrix@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _client(role: UserRole | None = None, settings: Settings | None = None) -> TestClient:
    """A wired app; ``role=None`` leaves the app unauthenticated.

    Both authentication dependencies are overridden: the router-level
    ``forge_api.deps.get_current_principal`` and the auth service's own
    ``get_authenticated_principal`` (the /auth router keys off the latter).
    """
    from forge_api.auth.service import get_authenticated_principal

    app = create_app(settings or Settings())
    if role is not None:
        resolved = _principal(role)
        app.dependency_overrides[get_current_principal] = lambda: resolved
        app.dependency_overrides[get_authenticated_principal] = lambda: resolved
    return TestClient(app)


# --------------------------------------------------------------------------- #
# RBAC                                                                         #
# --------------------------------------------------------------------------- #


def check_rbac_default_deny() -> None:
    from forge_api.auth.rbac import (
        ROLE_PERMISSIONS,
        Permission,
        PermissionDeniedError,
        can,
        ensure,
        permissions_for,
    )

    expected: dict[UserRole, frozenset[Permission]] = {
        UserRole.ADMIN: frozenset(Permission),
        UserRole.MEMBER: frozenset({Permission.READ, Permission.WRITE, Permission.RUN_AGENT}),
        UserRole.VIEWER: frozenset({Permission.READ}),
        UserRole.AGENT_RUNNER: frozenset({Permission.READ, Permission.RUN_AGENT}),
    }
    assert expected == ROLE_PERMISSIONS, "the authoritative RBAC matrix drifted"

    # Full cartesian product: can()/ensure() agree with the matrix everywhere.
    for role in UserRole:
        for permission in Permission:
            allowed = permission in expected[role]
            assert can(role, permission) is allowed, (role, permission)
            if allowed:
                ensure(role, permission)
            else:
                with pytest.raises(PermissionDeniedError):
                    ensure(role, permission)

    # An unknown/absent role gets the empty set (default deny).
    assert permissions_for("bogus-role") == frozenset()  # type: ignore[arg-type]


def check_rbac_wired_403() -> None:
    write_payload = [{"title": "t", "description": "d"}]
    # WRITE-gated route: viewer + agent-runner are denied, member/admin pass auth z.
    for role in (UserRole.VIEWER, UserRole.AGENT_RUNNER):
        with _client(role) as client:
            resp = client.post("/board/tasks/bulk", json=write_payload)
        assert resp.status_code == 403, (role, resp.status_code)
    for role in (UserRole.MEMBER, UserRole.ADMIN):
        with _client(role) as client:
            resp = client.post("/board/tasks/bulk", json=write_payload)
        assert resp.status_code != 403, (role, resp.status_code)

    # MANAGE_KEYS-gated route: member is denied too; only admin passes.
    for role in (UserRole.VIEWER, UserRole.AGENT_RUNNER, UserRole.MEMBER):
        with _client(role) as client:
            resp = client.get("/auth/api-keys")
        assert resp.status_code == 403, (role, resp.status_code)
    with _client(UserRole.ADMIN) as client:
        resp = client.get("/auth/api-keys")
    assert resp.status_code == 200, resp.status_code


#: Whole routers that are a pre-authentication protocol surface (no API-key
#: auth anywhere), so they legitimately expose no 401 route.
PUBLIC_BY_DESIGN_ROUTERS: dict[str, str] = {
    "saml": "SAML is a pre-authentication SSO protocol (metadata/login/ACS/SLO)",
    "oidc": "OIDC is a pre-authentication SSO protocol (login/callback); the "
    "admin config GET/PUT live on the authenticated sso_admin router",
}

#: Routes that are anonymous BY DESIGN, each with the reviewed reason.
PUBLIC_BY_DESIGN: dict[str, str] = {
    "/auth/callback": "OAuth redirect target: the IdP cannot send credentials",
    "/auth/bootstrap": "first-run bootstrap mints the initial admin key once",
    "/auth/session": "session login exchanges its own credential (not API-key auth)",
    "/auth/login": "session bootstrap (pre-authentication)",
    "/auth/logout": "stateless logout is idempotent; the client just discards its token",
    "/saml/metadata": "SP metadata is public by SAML design (no secrets inside)",
    "/saml/acs": "IdP-POSTed assertion is its own signed credential",
    "/saml/login": "SP-initiated login redirect (pre-authentication)",
    "/integrations/webhooks/github": "webhook auth = HMAC signature over raw bytes",
    "/integrations/webhooks/pm": "webhook auth = shared-secret header",
    "/alerts/webhooks/pagerduty": "webhook auth = per-provider HMAC signature",
    "/alerts/webhooks/datadog": "webhook auth = per-provider HMAC signature",
    "/alerts/webhooks/sentry": "webhook auth = per-provider HMAC signature",
    "/alerts/webhooks/grafana": "webhook auth = per-provider HMAC signature",
}


def check_auth_required_401() -> None:
    from fastapi.routing import APIRoute

    from forge_api.routers import FEATURE_ROUTERS

    # Disable rate limiting: this sweep probes every route on every router (well
    # over the burst budget), and a 429 partway through would mask the 401 we
    # are asserting. The rate limit is its own matrix row (ratelimit-429).
    app = create_app(Settings(ratelimit_enabled=False))
    offenders: list[str] = []
    saw_401: set[str] = set()
    with TestClient(app, raise_server_exceptions=False) as client:
        for router in FEATURE_ROUTERS:
            tag = str(router.tags[0]) if router.tags else repr(router)
            for route in router.routes:
                if not isinstance(route, APIRoute):
                    continue
                path = route.path
                if path in PUBLIC_BY_DESIGN:
                    continue
                # Fill path params with syntactically plausible values.
                probe = path
                for param in route.param_convertors:
                    probe = probe.replace("{" + param + "}", str(uuid.uuid4()))
                method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
                resp = client.request(method, probe)
                if 200 <= resp.status_code < 300:
                    offenders.append(f"{method} {path} -> {resp.status_code}")
                if resp.status_code == 401:
                    saw_401.add(tag)
    assert not offenders, f"anonymous 2xx on protected routes: {offenders}"
    # Every feature router (bar the pre-auth protocol surfaces) must expose at
    # least one route that 401s cleanly.
    tags = {str(r.tags[0]) for r in FEATURE_ROUTERS if r.tags}
    missing = tags - saw_401 - set(PUBLIC_BY_DESIGN_ROUTERS)
    assert not missing, f"routers with no sampled 401 (auth wiring suspect): {missing}"


# --------------------------------------------------------------------------- #
# MCP + policy + agent                                                         #
# --------------------------------------------------------------------------- #


def check_mcp_write_default_deny() -> None:
    from forge_contracts import MCPConnection, MCPWriteForbiddenError
    from forge_mcp.client import MCPGatewayClient
    from forge_mcp.security import is_write_tool
    from forge_mcp.testing import FakeTransport
    from forge_mcp.transport import ToolSpec

    # Classification fails closed: unrecognised verbs and un-annotated tools
    # are writes; only an explicit read_only annotation (or read verb) passes.
    assert is_write_tool("merge_pull_request", None) is True
    assert is_write_tool("approve", None) is True
    assert is_write_tool("frobnicate_gadget", None) is True  # unknown verb -> write
    assert is_write_tool("delete_repo", ToolSpec(name="delete_repo")) is True
    assert is_write_tool("get_issue", ToolSpec(name="get_issue", read_only=True)) is False

    # Wired path: a read-only connection refuses the write before transport I/O.
    transport = FakeTransport(
        tools=[ToolSpec(name="merge_pull_request"), ToolSpec(name="get_issue", read_only=True)],
        tool_results={"merge_pull_request": {"merged": True}, "get_issue": {"n": 1}},
    )
    client = MCPGatewayClient(transport)
    client.connect(MCPConnection(id="c1", name="gh"))  # allow_write defaults False
    with pytest.raises(MCPWriteForbiddenError):
        client.call_tool("merge_pull_request", {"pr": 1})
    assert transport.calls == [], "write reached the transport despite read-only conn"
    result = client.call_tool("get_issue", {"n": 1})
    assert result.status != "forbidden"


def check_policy_default_deny() -> None:
    from forge_contracts import DecisionEffect, Policy, ToolCall, WriteRules
    from forge_policy.evaluator import evaluate

    policy = Policy(
        repo_id="r1",
        allowed_actions=["read_file"],
        write_rules=WriteRules(allow=["app/**"], deny=["secrets/**"]),
    )
    # Unlisted action -> DENY (default deny).
    assert evaluate(ToolCall(tool="launch_missiles"), policy).effect is DecisionEffect.DENY
    # Write outside the allowlist -> DENY.
    assert (
        evaluate(ToolCall(tool="write_file", path="infra/main.tf"), policy).effect
        is DecisionEffect.DENY
    )
    # Path traversal -> DENY even under an allowed glob.
    assert (
        evaluate(ToolCall(tool="write_file", path="app/../secrets/k.pem"), policy).effect
        is DecisionEffect.DENY
    )
    # Empty/unidentifiable call -> DENY.
    assert evaluate(ToolCall(tool=""), policy).effect is DecisionEffect.DENY


def check_agent_policy_gate() -> None:
    from forge_agent.policy_gate import ActionPolicyGate
    from forge_agent.runtime import AgentRunner
    from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
    from forge_agent.tools import ToolRegistry, ToolResult
    from forge_contracts import AgentObjective, RunStatus

    executed: list[str] = []
    tools = ToolRegistry()
    tools.add(
        "delete_everything",
        lambda args: (executed.append("delete_everything"), ToolResult(ok=True))[1],
    )
    model = ScriptedModelClient([tool_response("delete_everything"), finish_response("done")])
    runner = AgentRunner(model, tools=tools, gate=ActionPolicyGate())
    result = runner.run(
        AgentObjective(
            objective="try a restricted tool",
            restricted_actions=["delete_everything"],
        )
    )
    assert executed == [], "policy-denied tool was executed by the agent"
    assert isinstance(result.status, RunStatus)


# --------------------------------------------------------------------------- #
# Redaction / secrets / audit                                                  #
# --------------------------------------------------------------------------- #


def check_redaction_sinks() -> None:
    from forge_api.observability.audit import AuditCategory, AuditLog
    from forge_api.observability.redaction import redact_mapping, redact_text, redact_value
    from forge_mcp.security import redact as mcp_redact
    from forge_mcp.security import redact_text as mcp_redact_text

    payload = {
        "authorization": _BEARER,
        "api_key": _SK_KEY,
        "jwt": _JWT,
        "aws_access_key": _AWS_KEY,
        "note": f"header {_BEARER}; key {_SK_KEY}; jwt {_JWT}; aws {_AWS_KEY}",
    }
    # The bare token part must vanish too (Bearer redaction strips the whole run).
    secrets = [_BEARER_TOKEN, _SK_KEY, _JWT, _AWS_KEY]
    detail = f"authz {_BEARER} key {_SK_KEY} jwt {_JWT} aws {_AWS_KEY}"

    # Log/trace sink (forge_api.observability.redaction).
    flat = str(redact_mapping(dict(payload))) + redact_text(str(payload))
    # Audit sink: detail flows through redact_text, metadata through
    # redact_mapping, and the raw payload is stored only as a hash.
    log = AuditLog()
    entry = log.record(
        category=AuditCategory.MCP_CALL,
        action="tool.call",
        actor="agent",
        workspace_id=WORKSPACE_ID,
        detail=detail,
        payload=dict(payload),
        metadata=dict(payload),
    )
    audit_repr = entry.model_dump_json()
    # MCP query-through snapshot sink: the transport redactor
    # (forge_mcp.security.redact) runs first, then the observability redactor
    # scrubs again before the snapshot lands in any trace/retrieval sink —
    # defense-in-depth, exactly the wired pipeline (AC8: "re-using
    # forge_mcp.security.redact + forge_api.observability.redaction").
    mcp_snapshot = str(redact_value(mcp_redact(dict(payload)))) + redact_text(
        mcp_redact_text(detail)
    )

    for sink_name, sink in [("trace", flat), ("audit", audit_repr), ("mcp", mcp_snapshot)]:
        for secret in secrets:
            assert secret not in sink, f"secret {secret[:8]}... survived in the {sink_name} sink"


def check_vault_no_plaintext() -> None:
    from forge_api.auth.crypto import FernetCipher, InvalidTokenError, generate_key
    from forge_api.auth.vault import SecretVault
    from forge_contracts import APIKeyKind

    plaintext = "sk-byok-very-secret-value-42"
    vault = SecretVault(cipher=FernetCipher(b"k" * 32))
    info = vault.put_secret(
        workspace_id=WORKSPACE_ID, name="prov", kind=APIKeyKind.MODEL_PROVIDER, secret=plaintext
    )
    record = vault.raw_record(WORKSPACE_ID, info.id)

    assert record.ciphertext != plaintext.encode()
    assert plaintext not in repr(record)
    assert plaintext.encode() not in record.ciphertext
    assert plaintext not in info.model_dump_json()
    assert vault.get_secret(WORKSPACE_ID, info.id) == plaintext

    # Wrong-key decrypt is an authenticated failure, not a padding oracle.
    with pytest.raises(InvalidTokenError):
        FernetCipher(generate_key()).decrypt(record.ciphertext)


def check_tenant_isolation() -> None:
    from forge_api.auth.crypto import FernetCipher
    from forge_api.auth.vault import SecretNotFoundError, SecretVault
    from forge_contracts import APIKeyKind

    vault = SecretVault(cipher=FernetCipher(b"t" * 32))
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws_a, name="a-key", kind=APIKeyKind.MODEL_PROVIDER, secret="a-secret"
    )

    assert [s.id for s in vault.list_secrets(ws_b)] == []
    with pytest.raises(SecretNotFoundError):
        vault.get_secret(ws_b, info.id)
    assert vault.get_secret(ws_a, info.id) == "a-secret"


def check_crypto_default_fernet() -> None:
    from forge_api.auth.crypto import FernetCipher, default_cipher

    cipher = default_cipher(b"m" * 32)
    assert isinstance(cipher, FernetCipher)
    assert cipher.decrypt(cipher.encrypt("round-trip")) == "round-trip"


def check_secret_key_required_prod() -> None:
    import os
    from unittest import mock

    from forge_api.auth.service import _resolve_master_key

    env = {k: v for k, v in os.environ.items() if k != "FORGE_SECRET_KEY"}
    env["FORGE_ENVIRONMENT"] = "production"
    with (
        mock.patch.dict(os.environ, env, clear=True),
        pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"),
    ):
        _resolve_master_key(None)


def check_audit_tamper_evident() -> None:
    from forge_api.observability.audit import AuditCategory, AuditLog, verify_chain

    log = AuditLog()
    for i in range(3):
        log.record(
            category=AuditCategory.AGENT_ACTION,
            action=f"step.{i}",
            actor="agent",
            workspace_id=str(WORKSPACE_ID),
            payload={"i": i},
        )
    assert log.verify_integrity() is True

    entries = log.store.all()
    tampered = entries[1].model_copy(update={"action": "step.FORGED"})
    assert verify_chain([entries[0], tampered, entries[2]]) is False

    # Append-only surface: the store exposes no update/delete API.
    mutators = [n for n in dir(log.store) if n.startswith(("update", "delete", "remove"))]
    assert mutators == [], f"audit store grew mutation methods: {mutators}"


def check_webhook_signature_fail_closed() -> None:
    from forge_integrations.webhooks import sign_github_payload, verify_github_signature

    secret, body = "whsec_test", b'{"action":"opened"}'
    good = sign_github_payload(secret, body)
    assert verify_github_signature(secret, body, good) is True
    assert verify_github_signature(secret, body, None) is False
    assert verify_github_signature(secret, body, "sha256=" + "0" * 64) is False
    assert verify_github_signature(secret, b'{"action":"tampered"}', good) is False


# --------------------------------------------------------------------------- #
# Edge controls                                                                #
# --------------------------------------------------------------------------- #


def check_ssrf_guard() -> None:
    from forge_api.security import SsrfBlockedError, assert_safe_url
    from forge_knowledge.embeddings import HttpEmbeddingClient
    from forge_knowledge.reranker import JinaRerankerClient

    blocked = [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1:5432/",
        "http://10.0.0.5/v1",
        "http://172.16.3.4/v1",
        "http://192.168.1.10/v1",
        "http://[::1]/v1",
        "http://[fd00::1]/v1",
        "http://0.0.0.0/v1",
        "file:///etc/passwd",
        "gopher://93.184.216.34/",
    ]
    for url in blocked:
        with pytest.raises(SsrfBlockedError):
            assert_safe_url(url)

    # Public + allowlisted hosts pass (IP literal avoids DNS in offline CI).
    assert_safe_url("https://93.184.216.34/v1")
    assert_safe_url("http://embedder.internal/v1", allowlist=["embedder.internal"])
    # allow_private admits RFC1918 but never metadata/loopback.
    assert_safe_url("http://10.0.0.5/v1", allow_private=True)
    with pytest.raises(SsrfBlockedError):
        assert_safe_url("http://169.254.169.254/x", allow_private=True)
    with pytest.raises(SsrfBlockedError):
        assert_safe_url("http://127.0.0.1/x", allow_private=True)

    # The DI seam enforces the guard inside the leaf clients (worker path).
    with pytest.raises(SsrfBlockedError):
        HttpEmbeddingClient(
            "text-embedding-3-small",
            base_url="http://169.254.169.254/v1",
            url_validator=assert_safe_url,
        )
    with pytest.raises(SsrfBlockedError):
        JinaRerankerClient(
            "jina-reranker-v2",
            base_url="http://169.254.169.254/v1",
            url_validator=assert_safe_url,
        )


def check_ratelimit_429() -> None:
    settings = Settings(ratelimit_enabled=True, ratelimit_rpm=60, ratelimit_burst=3)
    with _client(UserRole.ADMIN, settings) as client:
        responses = [client.get("/board/tasks") for _ in range(6)]
        codes = [r.status_code for r in responses]
        assert codes[0] != 429, "burst budget not honoured"
        first_limited = next((r for r in responses if r.status_code == 429), None)
        assert first_limited is not None, f"never rate-limited: {codes}"
        assert first_limited.headers.get("retry-after"), "429 without Retry-After"
        # /health stays exempt even while the caller is limited.
        assert all(client.get("/health").status_code == 200 for _ in range(5))


def check_bodylimit_413() -> None:
    settings = Settings(max_body_bytes=256)
    with _client(UserRole.ADMIN, settings) as client:
        resp = client.post(
            "/knowledge/search",
            content=b"{" + b"x" * 512 + b"}",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 413, resp.status_code


def check_security_headers() -> None:
    with _client(None) as client:
        resp = client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "max-age=" in resp.headers.get("strict-transport-security", "")
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert "default-src 'none'" in resp.headers.get("content-security-policy", "")


def check_docs_lockdown_prod() -> None:
    with _client(None, Settings(environment="production")) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path
    # Explicit operator opt-in still works.
    with _client(None, Settings(environment="production", docs_enabled=True)) as client:
        assert client.get("/docs").status_code == 200
    # Non-production keeps the default developer experience.
    with _client(None, Settings()) as client:
        assert client.get("/docs").status_code == 200


def check_principal_fails_safe() -> None:
    from forge_api.auth.rbac import Permission, can

    principal = Principal(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    assert principal.role is UserRole.VIEWER
    for permission in (
        Permission.WRITE,
        Permission.RUN_AGENT,
        Permission.MANAGE_KEYS,
        Permission.MANAGE_SECRETS,
        Permission.MANAGE_MEMBERS,
        Permission.ADMIN,
    ):
        assert not can(principal.role, permission), permission


def check_cors_lockdown() -> None:
    assert "*" not in Settings().cors_origins
    settings = Settings(cors_origins=["*"], cors_allow_credentials=True)
    with TestClient(create_app(settings)) as client:
        resp = client.get("/health", headers={"Origin": "https://evil.example"})
    acao = resp.headers.get("access-control-allow-origin")
    acac = resp.headers.get("access-control-allow-credentials")
    assert acao != "https://evil.example"
    assert not (acao == "*" and acac == "true")


#: check-name -> callable; consumed by test_enforcement_matrix.py.
OFFLINE_CHECKS: dict[str, Callable[[], None]] = {
    name: fn for name, fn in list(globals().items()) if name.startswith("check_") and callable(fn)
}
