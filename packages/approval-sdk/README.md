# forge-approval

The canonical Review & Approval Layer (slice F36 — human-approval-system).

One gate primitive backs every approval gate type in the spec (`spec`, `plan`,
`pr`, `deploy`, `incident_remediation`, `policy_override`):

- `ApprovalService` — create / list / get / get_context / resolve / count, with
  idempotent create, per-approver decision records, audit + activity events.
- `ApprovalAuthorizer` — the single server-side authorization policy: agents and
  system principals never resolve, viewers never resolve, `policy_override` is
  admin-only, repo `review_rules` apply to `pr`, deploy permission applies to
  `deploy`, optional no-self-approval.
- `GateRegistry` — per-gate `GateContextProvider` (the nine "must-show" items)
  and `GateResolutionHook` (the side effect on approve) plug-in points; gate
  owners register at the composition root, so this package depends on nothing
  above `forge-contracts`.
- `providers/` — the two gate primitives no other slice owns: `deploy` and
  `policy_override` (with the single-use, short-TTL `PolicyOverrideGrant`).

Persistence, HTTP, and Celery live in the apps; this package is pure domain.
