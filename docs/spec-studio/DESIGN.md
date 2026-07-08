# Spec Studio + Adaptive Orchestration — Design

Status: **Adaptive Orchestration is built and merged** (see
`docs/ADAPTIVE_SPEC_PROGRESS.md` for the slice-by-slice ledger). **Spec Studio**
(dual-format spec authoring UI, `spec.md` round-trip, and real-time
co-editing) is **design-approved but not yet implemented** — this document is
the design the next build phase implements against. It records the decisions
so implementation can proceed in independently-shippable slices, the same way
Adaptive Orchestration did.

## 1. Why one document for two features

Spec Studio and Adaptive Orchestration are separate surfaces but share one
idea: **a human-editable, agent-editable artifact with one canonical model
underneath**. Spec Studio applies that to the spec (`SpecManifest` canonical,
`spec.md`/`manifest.yaml` are views onto it); Adaptive Orchestration applies it
to *how* agents run against that spec (a policy sizes the work, a router picks
a model, a per-role config lets a human override either). They are designed
together so a Spec Studio edit that changes scope can re-trigger Adaptive
Orchestration's sizing without a second integration point.

## 2. Spec Studio — dual-format spec authoring

### 2.1 Canonical model, two serializations

`forge_contracts.SpecManifest` (frozen Pydantic DTO) is canonical. Two
first-class, human-and-agent-editable serializations round-trip through it:

- **`spec.md`** — YAML frontmatter + prose sections, the default
  human/agent authoring surface:

  ```markdown
  ---
  id: SPEC-042
  name: Adaptive rate limiting
  status: draft
  ---

  ## Goal

  One or two sentences: what this spec achieves and why.

  ## Requirements

  - R1: The gateway MUST reject requests over the configured budget.
  - R2: Limits MUST be configurable per workspace.

  ## Acceptance Criteria

  - AC1 (R1): Given a workspace at its budget, When a request arrives,
    Then the gateway returns 429 with a Retry-After header.
  - AC2 (R2): Given an admin sets a new limit, When the next request
    arrives, Then the new limit is enforced within one second.

  ## Constraints

  - Must not add a new datastore dependency.

  ## Open Questions

  - Q1: Should limits apply per-API-key or per-workspace?

  ## Decisions

  - D1: Per-workspace, matching the existing cost-ledger scope.
  ```

- **`manifest.yaml`** — the precise machine/CI/agent format, already
  implemented today (`forge_spec.manifest.dump_manifest` /
  `load_manifest`, `MANIFEST_FILENAME = "manifest.yaml"`).

A spec may be **created and edited from either format**. Editing one:

1. Parses the edited format back into a `SpecManifest` (validating it —
   unknown requirement refs in an AC, duplicate IDs, etc. are rejected with
   the same errors the engine already raises for `manifest.yaml`).
2. Re-renders the *other* format from the updated manifest, so the two files
   never drift.

Legacy manifest-only specs (no `spec.md`, or a `spec.md` predating the
frontmatter/Goal/Decisions sections) still load: the loader falls back to the
existing one-way `render_spec_md` output when no frontmatter is present, and
upgrades the file to the new round-trippable shape on first save.

### 2.2 What exists today vs. what this design adds

| Piece | State |
|---|---|
| `SpecManifest` canonical DTO | **Shipped** (`forge_contracts`) |
| `manifest.yaml` round-trip (`dump_manifest`/`load_manifest`) | **Shipped** (`forge_spec.manifest`) |
| `spec.md` rendering (`render_spec_md`) | **Shipped, but one-way** — manifest → markdown only; no frontmatter, no `## Goal`, no Given/When/Then AC phrasing, no `## Decisions` section |
| `spec.md` **parsing** (`parse_spec_md`) | **Not implemented** — no code path reads edits back out of `spec.md` |
| Round-trip sync (edit either → both stay current) | **Not implemented** |
| `POST /spec/draft` (BYOK AI draft from a one-line goal) | **Not implemented** — no route, schema, or service exists |
| Spec Studio web UI (split/synced editor) | **Not implemented** — `apps/web` has a read-only `spec-dashboard` (validation view), not an editor |
| Real-time co-editing | **Not implemented** — no CRDT/OT dependency, no `/ws` route exists anywhere in `apps/api` |

The gap is intentionally scoped as follow-up slices:

- **`spec-md-roundtrip`** — add `parse_spec_md(text) -> SpecManifest`
  (frontmatter + section parser, tolerant of the legacy one-way shape) and
  extend `render_spec_md` to emit the full section set above; wire both
  through `FileSpecEngine` so `save_spec_md`/`save_manifest` converge on the
  same `SpecManifest.model_dump()` before writing either file, matching the
  existing `_write` pattern in `engine.py`.
- **`spec-draft-api`** — `POST /spec/draft` takes `{goal: str, project_id}`,
  resolves the `spec_author` role via the Adaptive Orchestration model
  router (§3), streams a constitution-seeded draft through the BYOK
  `ModelClient`, and returns a `spec.md` draft (never auto-saved — a human
  must accept it through the normal spec-engine write path). Tests mock
  `ModelClient`; no live key is exercised in CI.
- **`spec-studio-ui`** — a two-pane (rendered `spec.md` / editable form over
  the same fields) editor in `apps/web`, reusing `spec-dashboard`'s
  validation-report rendering for inline AC/requirement lint feedback.
- **`spec-studio-realtime`** — real-time co-editing (§4) once the editor
  above exists to co-edit.

### 2.3 Round-trip contract

`parse_spec_md` and `render_spec_md` must satisfy, for every `SpecManifest`
producible by the engine:

```
parse_spec_md(render_spec_md(m)) == m   (field-for-field, order-insensitive)
```

and for every well-formed `spec.md` a human might hand-edit:

```
render_spec_md(parse_spec_md(text))  round-trips the same requirements/ACs/
constraints/open-questions/decisions (whitespace and heading-order may be
normalized, content must not be lost)
```

This is the same guarantee `dump_manifest`/`load_manifest` already gives for
YAML today (`packages/spec-engine/tests` has the precedent fixtures to
extend).

## 3. Adaptive Orchestration (shipped — summarized here for cross-reference)

Full ledger and how-to-configure detail: `docs/ADAPTIVE_SPEC_PROGRESS.md`.
The load-bearing shape, for Spec Studio integration purposes:

```
spec/task  --(forge_orchestration_policy.complexity)-->  ComplexitySizing
                                                            {tier, strategy}
                    |
                    v
        forge_agent.execution_plan.ExecutionPlan.for_role(role)
                    |
                    v
   per-role override (AoSettingsService: workspace/project) or DEFAULT_ROLE_CONFIG
                    |
                    v
        forge_agent.providers.router.ModelRouter.resolve(tier) -> concrete model
                    |
                    v
              BYOK ModelClient (HARD-02, unchanged)
```

A Spec Studio edit that changes the requirements/AC count or a `## Decisions`
entry marking scope as "complex" is exactly the kind of signal
`forge_orchestration_policy.complexity` already consumes (it sizes off
requirement/AC counts and free-text risk keywords) — no new integration point
is needed; re-running the sizing after a save is a Spec Studio-side call to
the existing policy, not a new contract.

## 4. Real-time co-editing — library choice (design decision, not yet wired)

**Decision: Yjs**, with `y-websocket`'s wire protocol (not necessarily its
server) as the sync transport, for the following reasons weighed against the
alternatives:

| Option | Verdict |
|---|---|
| **Yjs** | **Chosen.** CRDT (conflict-free by construction — no central lock/OT-transform server needed); mature `y-prosemirror` / plain-text bindings for a markdown editor; small, dependency-free core (`yjs` has zero runtime deps); works offline and merges on reconnect, which matters for an agent that may edit `spec.md` on disk while a human has the tab open. |
| Automerge | Considered. Also a CRDT with a Rust/WASM core; heavier bundle (WASM payload) for marginal gains over Yjs for structured-text docs; Yjs has the more battle-tested text/markdown editor bindings. |
| Operational Transform (ShareDB, etc.) | Rejected. Requires a central sequencing server (single point of failure, and conflicts with the existing stateless FastAPI/worker split); harder to reconcile with an agent writing the same file through the filesystem-backed `FileSpecEngine` outside the OT server's view. |
| Vendor real-time (Liveblocks, PartyKit) | Rejected. Adds an external network dependency Forge's self-hosted deploy story (Compose/Helm) does not have a slot for; conflicts with the BYOK/self-hosted posture the rest of the platform holds to. |

Integration shape (for the `spec-studio-realtime` slice):

- **Transport**: the `/ws` route noted as deferred in
  `docs/MORNING_SUMMARY-2026-07-08.md` ("the server-side `/ws` websocket, as
  the `rt-ws` real-time slice") is the same substrate this feature needs — one
  websocket endpoint in `apps/api`, not a bespoke one for specs. A Yjs update
  is an opaque binary diff; the `/ws` route relays it between subscribers on
  the same `spec_id` room and does **not** need to parse it, keeping the
  server logic-free (no CRDT library needed server-side beyond relaying
  bytes — this is the standard Yjs "dumb pipe" deployment).
- **Persistence**: on a debounced interval (or on last-writer-disconnect),
  the resolved Yjs document text is parsed via `parse_spec_md` (§2) and
  written through the existing `FileSpecEngine` save path — the CRDT layer
  never becomes a second source of truth; the file (and the `SpecManifest`
  it parses to) stays canonical, matching the "ONE canonical model" design
  principle above.
- **Agent edits**: an agent editing the same spec (e.g. spec-author role)
  writes through `FileSpecEngine` as it does today; the `/ws` relay picks up
  the resulting file change (a filesystem watch or an explicit
  "agent wrote spec X" event) and applies it into the shared Yjs doc as a new
  update, so human and agent edits merge through the same CRDT rather than
  needing a bespoke merge path.
- **Dependency footprint**: `yjs` (JS, `apps/web`) + a thin Python relay with
  no CRDT dependency at all (`apps/api`) — no new Python package is added to
  the `uv` workspace for this.

None of the above is implemented yet; this section is the target design the
`spec-studio-realtime` slice builds against.

## 5. Open questions carried into implementation

- **Q1**: Should `parse_spec_md` accept partial edits (e.g., a human deletes
  the `## Open Questions` section entirely) as "no open questions" or as
  invalid input requiring the section header to stay present with `_None_`?
  Leaning: absent section = empty list, matching `_bullets([])` already
  rendering `_None_` today.
- **Q2**: Presence/typing indicators for co-editing (who else is viewing) —
  Yjs's awareness protocol (`y-protocols/awareness`) covers this for free
  once the transport lands; not a separate build item, just needs enabling.
- **Q3**: Multi-workspace fan-out limits on the `/ws` relay (a noisy spec
  should not starve other rooms) — deferred to the `rt-ws` slice's own design,
  since the relay is shared infrastructure, not Spec-Studio-specific.
