# forge-auth

Pure auth & secrets crypto core for Forge (`cross-cutting/F37-auth-secrets-byok`).

No FastAPI / SQLAlchemy imports — mirrors the `policy-sdk` / `authz-sdk` discipline.

- `vault` — AES-256-GCM envelope encryption with a versioned master key (KEK)
  and an HKDF-derived per-workspace data key (DEK); the workspace id is bound
  into the GCM AAD so a ciphertext is useless outside its workspace. Supports
  KEK rotation.
- `keys` — platform API-key generate / hash / verify / parse (`forge_pat_…` /
  `forge_svc_…` / `forge_agt_…` tokens with an embedded public `key_id`;
  one-way peppered HMAC-SHA256 hashing, constant-time verify).
- `tokens` — HS256 session JWT encode/decode (`SessionClaims`) + the internal
  service token.
- `rbac` — the flat V1 role ranking (`admin > member > {agent-runner, viewer}`).
- `redaction` — the canonical `SecretRedactor` (patterns + entropy + a dynamic
  known-secret registry).
- `ratelimit` — an in-process fixed-window `RateLimiter` implementation.
