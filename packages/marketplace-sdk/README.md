# forge-marketplace

The **integration-marketplace SDK** (slice F32). A thin, security-critical layer
on top of the existing F09 MCP-connector and F11 skill-profile artifacts that
turns them into a discoverable, versioned, provenance-tracked distribution
channel.

The SDK is **pure** (no FastAPI, no DB, no live network in its core): it owns the
trust boundary — canonical hashing, Ed25519 signature verification, fail-closed
schema validation via the authoritative F09/F11 loaders, and the least-privilege
install security floor. The `apps/api` service + `apps/worker` tasks own the DB
records and orchestration.

Key modules:

* `models` — Pydantic v2 models + enums (the `forge-package.yaml` manifest, the
  registry `index.json`, install plan/result DTOs).
* `manifest` — canonical artifact encoding + `content_hash` + fail-closed loader.
* `index` — registry index parsing (`extra='forbid'`).
* `verifier` — Ed25519 detached-signature verifier + verification precedence.
* `installer` — per-kind validation + the MCP read-only/pending security floor.
* `catalog` — semver compare, `min_forge_version` gate, latest-compatible + update.
* `registry_client` — SSRF-bounded fetch guard.
* `packaging` — `build_package` (the author-side `forge marketplace package`).

No package ever executes code — artifacts are declarative YAML only.
