# forge-authz

Pure, total RBAC permission resolver for Forge (F30 — multi-team workspace
controls & full RBAC hierarchy).

`forge_authz` has **no** FastAPI / SQLAlchemy imports (it mirrors `forge_policy`):
it consumes the frozen `forge_contracts.authz` DTOs and computes a principal's
effective permission set on a resource under an explicit, documented precedence
rule. The same `DefaultPermissionResolver` powers the API dependency layer, the
effective-access inspector, and the agent runtime's scope checks.

* `permissions.py` — the frozen `ROLE_PERMISSIONS` / `ACCESS_LEVEL_ROLE` /
  `ROLE_RANK` tables + `scope_narrow`.
* `resolver.py` — `DefaultPermissionResolver` (pure `resolve()` / `can()`) plus
  the escalation / lockout / team-cycle / team-depth invariants.
* `errors.py` — `AccessDenied`, `EscalationError`, `LastAdminError`,
  `TeamCycleError`, `TeamDepthError`.
* `schema.py` — re-exports the contract DTOs so the resolver and the contract
  share one object set.
