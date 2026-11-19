"""Offline enforcement-matrix checks (HARD-09).

Each function here is named by a row in ``security/enforcement-matrix.yaml``
(`check:` key) and asserts one SCF_SPEC security control **on the wired
path** — route -> dependency -> primitive — so a control that is implemented
but never mounted is still a red test. Pure asserts, no fixtures, no network,
no DB, no external creds (live-db rows live in ``test_enforcement_matrix.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.settings import Settings
from scf_contracts import UserRole

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


def _app(
    role: UserRole | None = None,
    settings: Settings | None = None,
    db: sessionmaker[Session] | None = None,
) -> FastAPI:
    """A wired app; ``role=None`` leaves the app unauthenticated.

    Both authentication dependencies are overridden: the router-level
    ``scf_api.deps.get_current_principal`` and the auth service's own
    ``get_authenticated_principal`` (the /auth router keys off the latter).
    ``db`` serves the request sessions, so a handler past its guard runs offline.
    """
    from scf_api.auth.service import get_authenticated_principal
    from scf_api.db import get_db

    app = create_app(settings or Settings())
    if role is not None:
        resolved = _principal(role)
        app.dependency_overrides[get_current_principal] = lambda: resolved
        app.dependency_overrides[get_authenticated_principal] = lambda: resolved
    if db is not None:

        def _session() -> Iterator[Session]:
            with db() as session:
                yield session

        app.dependency_overrides[get_db] = _session
    return app


def _client(role: UserRole | None = None, settings: Settings | None = None) -> TestClient:
    return TestClient(_app(role, settings))


def _sqlite() -> sessionmaker[Session]:
    """An in-memory database with the whole schema and this module's workspace."""
    from sqlalchemy import StaticPool, create_engine

    from scf_db.base import Base
    from scf_db.models import Workspace

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with db() as session:
        session.add(Workspace(id=WORKSPACE_ID, name="Matrix", slug="matrix"))
        session.commit()
    return db


# --------------------------------------------------------------------------- #
# RBAC                                                                         #
# --------------------------------------------------------------------------- #


def check_rbac_default_deny() -> None:
    from scf_api.auth.rbac import (
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

    # The workspace's GitHub App credentials (#186): MANAGE_KEYS too.
    route = "/integration/github/app-credentials"
    body = {"app_id": "1", "installation_id": "2", "private_key": _github_app_key()}
    for role in (UserRole.VIEWER, UserRole.AGENT_RUNNER, UserRole.MEMBER):
        with _client(role) as client:
            assert client.put(route, json=body).status_code == 403, role
            assert client.delete(route).status_code == 403, role
    with _client(UserRole.ADMIN) as client:
        assert client.delete(route).status_code == 204


def _github_app_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


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
    "/auth/logout": "idempotent: 204 without a session cookie; with one it revokes that "
    "cookie's own key and clears it (from SCF's own origin only)",
    "/auth/sign-in-link": "a browser opens the one-time link scf-cli admin sign-in-link "
    "printed, with no credentials: the link's token is the authentication, once (#260)",
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

    from scf_api.routers import FEATURE_ROUTERS

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


def check_route_role_matrix() -> None:
    from route_matrix import DECLARED, UNGUARDED, routes, url

    from scf_api.auth.rbac import permissions_for

    # A 429 partway through the sweep would mask the status under test.
    settings = Settings(ratelimit_enabled=False)
    walked = {f"{method} {path}": needs for method, path, needs in routes(create_app(settings))}
    # Every route and method of the running app is declared once: guarded, or with a reason.
    undeclared = sorted(walked.keys() - DECLARED.keys() - UNGUARDED.keys())
    assert not undeclared, f"routes with no declared expectation: {undeclared}"
    stale = sorted((DECLARED.keys() | UNGUARDED.keys()) - walked.keys())
    assert not stale, f"declared routes the app does not serve: {stale}"
    twice = sorted(DECLARED.keys() & UNGUARDED.keys())
    assert not twice, f"routes declared twice: {twice}"
    drift = [
        k for k, needs in walked.items() if needs != ({DECLARED[k]} if k in DECLARED else None)
    ]
    assert drift == [], f"guards other than declared: {drift}"

    # Every role that lacks the permission gets 403; every role that holds it gets past.
    db = _sqlite()
    wrong: list[str] = []
    for role in UserRole:
        held = permissions_for(role)
        with TestClient(_app(role, settings, db), raise_server_exceptions=False) as client:
            for key, permission in DECLARED.items():
                method, path = key.split(" ", 1)
                status = client.request(method, url(path)).status_code
                if status == 401 or (status == 403) is (permission in held):
                    wrong.append(f"{role.value}: {key} -> {status}")
    assert wrong == [], f"routes answering against the declared permission: {wrong}"


#: A body each workspace-selector route accepts, so the call reaches its handler.
_SELECTOR_BODIES: dict[str, Any] = {
    "PUT /workspaces/{workspace_id}/sso": {},
    "PUT /workspaces/{workspace_id}/oidc": {"issuer": "https://idp.example", "client_id": "c"},
    "POST /workspaces/{workspace_id}/sso/test": {"saml_response": "x"},
    "POST /workspaces/{workspace_id}/scim/tokens": {"name": "t"},
}

#: Routes that authenticate no principal yet take a workspace selector, each with why.
UNAUTHENTICATED_WORKSPACE_SELECTORS: dict[str, str] = {
    "POST /integrations/alerts/{provider}/webhook": (
        "OPEN DEFECT (#321): the workspace_id query is not covered by the provider's"
        " signature, which is over the body only and under one secret per deployment"
    ),
}


def check_workspace_selector_scoped() -> None:
    import os
    from unittest import mock

    from fastapi.routing import APIRoute, iter_route_contexts

    from scf_api.auth.service import get_authenticated_principal
    from scf_db.models import OidcConfiguration, ScimToken, SsoConfiguration, Workspace

    # Another workspace with something behind every selector route.
    other = uuid.uuid4()
    db = _sqlite()
    with db() as session:
        session.add(Workspace(id=other, name="Other", slug="other"))
        session.flush()
        token = ScimToken(workspace_id=other, name="t", token_hash="0" * 64, token_prefix="t")
        sso = SsoConfiguration(
            workspace_id=other,
            idp_entity_id="e",
            idp_sso_url="u",
            idp_x509_certs=["c"],
            sp_entity_id="e",
            sp_private_key_encrypted=b"k",
            sp_cert_pem="c",
        )
        oidc = OidcConfiguration(
            workspace_id=other,
            issuer="i",
            client_id="c",
            client_secret_ref="r",
            client_secret_encrypted=b"s",
        )
        session.add_all([token, sso, oidc])
        session.commit()
    fill = {"workspace_id": str(other), "token_id": str(token.id)}

    app = _app(UserRole.ADMIN, Settings(ratelimit_enabled=False), db)
    authenticators = {get_current_principal, get_authenticated_principal}
    seen: list[str] = []
    wrong: list[str] = []
    unauthenticated: set[str] = set()
    # The SSO config service keeps its keys under the master key.
    master_key = {"FORGE_SECRET_KEY": "matrix-selector-master-key-0123456789"}
    with (
        mock.patch.dict(os.environ, master_key),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        for route in iter_route_contexts(app.routes):
            if not isinstance(route.original_route, APIRoute):
                continue
            stack, deps = [route.dependant], []
            while stack:
                deps.append(stack.pop())
                stack.extend(deps[-1].dependencies)
            params = [p for d in deps for p in (*d.path_params, *d.query_params, *d.header_params)]
            named = {p.alias: str(other) for p in params if "workspace" in p.alias.lower()}
            if not named:
                continue
            # A route that reads no principal has no caller's workspace to hold it to,
            # so its selector must be accounted for by name, never skipped silently.
            if not any(dep.call in authenticators for dep in deps):
                unauthenticated.update(f"{m} {route.path}" for m in route.methods or ())
                continue
            probe = route.path
            for param in route.param_convertors:
                probe = probe.replace("{" + param + "}", fill.get(param, str(uuid.uuid4())))
            for method in sorted((route.methods or set()) - {"HEAD"}):
                key = f"{method} {route.path}"
                seen.append(key)
                body = _SELECTOR_BODIES.get(key)
                resp = client.request(method, probe, params=named, headers=named, json=body)
                if resp.status_code not in (403, 404):
                    wrong.append(f"{key} -> {resp.status_code}")
    assert len(seen) >= 11, f"the selector walk found too few routes: {seen}"
    assert wrong == [], f"routes acting on another workspace: {wrong}"
    assert unauthenticated == set(UNAUTHENTICATED_WORKSPACE_SELECTORS), (
        "unauthenticated routes taking a workspace selector must be listed with a reason: "
        f"{sorted(unauthenticated ^ set(UNAUTHENTICATED_WORKSPACE_SELECTORS))}"
    )


# --------------------------------------------------------------------------- #
# MCP + policy + agent                                                         #
# --------------------------------------------------------------------------- #


def check_mcp_write_default_deny() -> None:
    from scf_contracts import MCPConnection, MCPWriteForbiddenError
    from scf_mcp.client import MCPGatewayClient
    from scf_mcp.security import is_write_tool
    from scf_mcp.testing import FakeTransport
    from scf_mcp.transport import ToolSpec

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
    from scf_contracts import DecisionEffect, Policy, ToolCall, WriteRules
    from scf_policy.evaluator import evaluate

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
    from scf_agent.policy_gate import ActionPolicyGate
    from scf_agent.runtime import AgentRunner
    from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
    from scf_agent.tools import ToolRegistry, ToolResult
    from scf_contracts import AgentObjective, RunStatus

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
    from scf_api.observability.audit import AuditCategory, AuditLog
    from scf_api.observability.redaction import redact_mapping, redact_text, redact_value
    from scf_mcp.security import redact as mcp_redact
    from scf_mcp.security import redact_text as mcp_redact_text

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

    # Log/trace sink (scf_api.observability.redaction).
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
    # (scf_mcp.security.redact) runs first, then the observability redactor
    # scrubs again before the snapshot lands in any trace/retrieval sink —
    # defense-in-depth, exactly the wired pipeline (AC8: "re-using
    # scf_mcp.security.redact + scf_api.observability.redaction").
    mcp_snapshot = str(redact_value(mcp_redact(dict(payload)))) + redact_text(
        mcp_redact_text(detail)
    )

    for sink_name, sink in [("trace", flat), ("audit", audit_repr), ("mcp", mcp_snapshot)]:
        for secret in secrets:
            assert secret not in sink, f"secret {secret[:8]}... survived in the {sink_name} sink"


def check_vault_no_plaintext() -> None:
    from scf_api.auth.crypto import FernetCipher, InvalidTokenError, generate_key
    from scf_api.auth.vault import SecretVault
    from scf_contracts import APIKeyKind

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
    from scf_api.auth.crypto import FernetCipher
    from scf_api.auth.vault import SecretNotFoundError, SecretVault
    from scf_contracts import APIKeyKind

    vault = SecretVault(cipher=FernetCipher(b"t" * 32))
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws_a, name="a-key", kind=APIKeyKind.MODEL_PROVIDER, secret="a-secret"
    )

    assert [s.id for s in vault.list_secrets(ws_b)] == []
    with pytest.raises(SecretNotFoundError):
        vault.get_secret(ws_b, info.id)
    assert vault.get_secret(ws_a, info.id) == "a-secret"

    # Wired (#186): workspace B's admin can neither see nor clear A's GitHub App.
    from scf_api.auth.service import get_auth_service

    route = "/integration/github/app-credentials"
    body = {"app_id": "1", "installation_id": "2", "private_key": _github_app_key()}
    service = get_auth_service()
    clients = {}
    for ws in (ws_a, ws_b):
        app = create_app(Settings())
        admin = Principal(user_id=USER_ID, workspace_id=ws, role=UserRole.ADMIN)
        app.dependency_overrides[get_current_principal] = lambda admin=admin: admin
        clients[ws] = TestClient(app)
    try:
        assert clients[ws_a].put(route, json=body).status_code == 200
        assert clients[ws_b].delete(route).status_code == 204
        assert [s.name for s in service.vault.list_secrets(ws_a)] == ["github-app"]
        assert service.vault.list_secrets(ws_b) == []
    finally:
        clients[ws_a].delete(route)


def check_crypto_default_fernet() -> None:
    from scf_api.auth.crypto import FernetCipher, default_cipher

    cipher = default_cipher(b"m" * 32)
    assert isinstance(cipher, FernetCipher)
    assert cipher.decrypt(cipher.encrypt("round-trip")) == "round-trip"


def check_secret_key_required_prod() -> None:
    import os
    from unittest import mock

    from scf_api.auth.service import _resolve_master_key

    env = {k: v for k, v in os.environ.items() if k != "FORGE_SECRET_KEY"}
    env["FORGE_ENVIRONMENT"] = "production"
    with (
        mock.patch.dict(os.environ, env, clear=True),
        pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"),
    ):
        _resolve_master_key(None)


def check_audit_tamper_evident() -> None:
    from scf_api.observability.audit import AuditCategory, AuditLog, verify_chain

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
    from scf_integrations.webhooks import sign_github_payload, verify_github_signature

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
    from scf_api.security import SsrfBlockedError, assert_safe_url
    from scf_knowledge.embeddings import HttpEmbeddingClient
    from scf_knowledge.reranker import JinaRerankerClient

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
    from scf_api.auth.rbac import Permission, can

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
