# H — World model (utopia++)

**Date:** 2026-11-07
**Status:** H1 Approved, implementation-ready. H2–H4 DRAFT — scope and interfaces only.
**Depends on:** nothing (uses `forge_contracts.ModelClient`, `forge_db`, existing RBAC and audit)
**Blocks:** I (MCP server), J (context and memory)
**Decisions:** ADR-0003 (rebuild, not embed), ADR-0004 (bitemporal ledger on Postgres), ADR-0005
(agents propose, people decide), ADR-0009 (migration numbering), ADR-0010 (clean room), ADR-0011
(extraction through forge providers)

---

## 1. Problem

Forge's knowledge module retrieves **passages**. It cannot answer *what is true*, *since when*,
*according to whom*, or *what did we believe when we decided X*. Agents therefore re-derive facts
from text on every run, and nothing records which facts a decision rested on.

deeplethe/utopia solves the first three as a self-hosted Rust service: a two-clock, evidence-backed
fact ledger where agents propose and people decide. lore already routes its default context
reads to utopia because utopia scored **0.86** against lore's **0.57** on lore's own 60-question
agent benchmark. Running utopia beside forge would duplicate auth, users, UI, API contracts, the LLM
layer and a second search index (ADR-0003). This sub-project rebuilds utopia's essential behaviour
natively, and adds what utopia never built.

## 2. Approach

A new package `packages/world-model` (`forge_world`) holds **pure domain logic**: time, ledger
rules, temporal closing, extraction verification, identity recall, the governance gate, reasoning,
conflict detection and decision drift. `forge_db` gains the tables. `forge_api` gains a `/world`
router. Postgres is the only store (ADR-0004).

Rejected alternatives:

- **Run utopia as a sidecar.** Rejected in ADR-0003: two identity systems, a Vite SPA beside
  Next.js, a REST surface with "no stability promise", an on-disk Tantivy index outside Postgres
  backups, and an OpenAI-dialect-only model layer.
- **Extend `knowledge-core`.** Rejected: knowledge-core models *passages* (chunk, embed, retrieve).
  The world model models *claims about the world* with two clocks, identity and governance.
  Different invariants, different consumers; coupling them makes every retrieval change a ledger
  change. The world model *reads* knowledge-core's chunker; it does not live in it.
- **A graph database (Dgraph, AGE).** Rejected: lore's three-store split (Postgres, Dgraph, utopia)
  is one of its documented failure sources. Recursive CTEs and in-process fixed points cover H1–H3.

## 3. Model

### 3.1 Two clocks

Every fact carries a **world interval** (when it was true) and a **record interval** (when forge
believed it).

```
holds_at(f, at)    = (f.valid_from is None or f.valid_from <= at)
                     and (f.valid_to is None or at < f.valid_to)
held_at(f, as_of)  = f.recorded_at <= as_of
                     and (f.invalidated_at is None or as_of < f.invalidated_at)
```

Every read takes `at` and `as_of`, both defaulting to *now*. There is exactly one implementation of
each predicate, in `forge_world.time`, and one SQL rendering of each, in the repository. Nothing
else filters by time.

**Precision.** Each world bound has a precision from the ladder
`year | month | day | hour | minute | second | unknown`. A bound is stored as an instant plus its
precision: a `valid_from` is the **start** of its bucket, a `valid_to` the **exclusive end** of its
bucket. "Left in 2024" becomes `valid_to = 2025-01-01T00:00Z, precision = year,
end_after_period = true`. `unknown` stores `NULL`.

**Relation kinds.** `state` facts span an interval. `event` facts collapse to one bucket
(`valid_to` = end of `valid_from`'s bucket). `eternal` facts carry no dates. A `state` span whose
two ends are equal is refused.

### 3.2 The ledger is append-only with a one-way tombstone

`world_fact` rows are never updated, except for one transition: `invalidated_at` and
`invalidated_reason` move from `NULL` to a value, once. A Postgres trigger enforces exactly that
(§5.3); DELETE always raises. A correction is *invalidate old + insert new with
`supersedes_id = old`*, in one transaction, in that order.

`invalidated_reason ∈ {superseded, retracted, underived, source_deleted, merged_away}`.

**Dedup is a constraint, not a check (++).** `dedup_key = sha256(subject_id | normalised predicate
or phrase | object_id or normalised value | mood | layer | valid_from | valid_to)`. A **partial
unique index** on `(workspace_id, dedup_key) WHERE invalidated_at IS NULL` makes a duplicate live
fact impossible even under parallel ingest — utopia's read-then-write dedup has a documented race.
Inserting a duplicate returns the existing live fact; re-asserting a previously invalidated fact is
allowed.

### 3.3 Layers, mood, provenance

- `layer ∈ {open, typed, derived, memory}`. `open` = predicate is the source phrase only; `typed` =
  bound to a declared relation; `derived` = produced by reasoning; `memory` = agent/person memory
  (J builds its lifecycle on this layer).
- `mood ∈ {NULL, plan, requirement, condition}`. Mood facts never close other facts, never
  participate in reasoning, and are always shown with their mood.
- Every fact records `source_kind ∈ {document, person, agent, rule, mcp}`, `actor`, optional
  `proposal_id`, and — for `document` facts — at least one `world_evidence` row with a verbatim
  quote and **server-computed** offsets. The model never supplies offsets.

### 3.4 Ontology (minimal in H1)

- `world_class(key, label, parent_key, disjoint_with[], iri)`.
- `world_relation(key, label, kind, domain_class, range_class, functional, inverse_functional,
  transitive, symmetric, inverse_of, sub_property_of, iri)`. `range_class = 'value'` means a
  literal.
- `key` matches `^[a-z0-9_]{1,40}$` and is the API token; `iri` is optional identity for import
  (H2).
- One system relation exists in every workspace without declaration: `known_as` (state, range
  `value`) — every name is a fact.
- Typing is optional: `world_entity.type_key` is nullable; `type_source = 'person'` is final and
  never overwritten by a model.

### 3.5 Identity (H1 subset)

- An entity is created on first sight of a **named** thing. Its names are `known_as` facts with
  evidence.
- **Recall channel 1:** exact match, then suffix-stripped match (`inc, ltd, llc, gmbh, corp,
  corporation, company, co, plc, ag, sa, bv`, trailing punctuation, case, whitespace), restricted to
  a compatible type (same class, ancestor/descendant, or either untyped). Classes declared disjoint
  never match.
- A **described** thing ("the vendor") gets an entity with `described = true` and
  `described_in = source_ref`. It gets no `known_as` fact and is never recalled from another
  document.
- **Merge** records `world_entity_merge(survivor_id, merged_id)`; **revert** stamps `reverted_at`.
  Reads resolve an id to its survivor when a merge is live at `as_of`, following chains up to 8
  hops. A merge is refused when the classes are disjoint, the ids are equal, or `merged_id` already
  has a live merge. At most one live merge per `merged_id` (partial unique index).
- Channel 2 (name vectors), context vectors and the model adjudicator are H2.

### 3.6 Temporal closing

Applies only to relations with `kind = state` and `functional = true` (or `inverse_functional`,
mirrored on the object side). For one `(subject, predicate)`:

1. Take the live, non-mood facts.
2. Order by `valid_from`; facts with unknown start are **not** ordered (see 4).
3. For each consecutive pair (a, b) with different values where `a.valid_to is None or
   a.end_derived`: a's derived end is `b.valid_from`, marked `end_derived = true`.
4. A pair where one start is unknown closes nothing and is reported as a conflict for a person.
5. The diff between current and computed ends is applied as supersessions.

The computation is a pure function of the fact set, so the result does not depend on arrival
order. A successor whose start is unknown closes nothing. Two values holding at the same moment
are a **conflict** (§3.9); a succession is not.

### 3.7 Extraction

`ingest(source_ref, title, text, doc_time)`:

1. Chunk the text with `forge_knowledge.chunking` (≈300 tokens, no overlap, heading breadcrumbs).
2. For each chunk, one `ModelClient.complete` call at `temperature = 0` (ADR-0011) with: the
   title, the document's first 1,500 characters, the names already recorded from this document, and
   the chunk. No ontology and no document date go in the prompt.
3. The reply contract is JSON: `{"statements": [{"quote", "subject", "subject_kind", "phrase",
   "object", "value", "object_kind", "time": {"from", "to", "words"}, "mood", "qualifiers"}]}`, with
   the quote first.
4. **Server verification** — each failure is a coded drop, stored as a `world_extraction_drop` row:
   `quote_not_in_chunk`, `name_not_in_text`, `time_not_in_quote`, `malformed_item`,
   `truncated_reply`, `chunk_unextracted` (the model call failed).
5. Time: `from`/`to` accept `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or a full ISO-8601 instant; precision
   comes from the form. Relative words ("last year") are kept as `unknown` in H1 (resolved against
   `doc_time` in H2). `doc_time` is never upload time.
6. Accepted statements are written as `open` facts (or `typed`, when the phrase equals exactly one
   declared non-state relation's key or label) with `source_kind = document` and evidence.

Truncated replies are salvaged item by item; a reply that is not JSON at all drops the chunk with
`truncated_reply` or `malformed_item`.

### 3.8 Proposals and the governance gate

- Agents, MCP clients and passive capture **never write facts**. They create a
  `world_proposal(kind = fact)` with the statement and a rationale. Rationale: a document can say
  "remember that X" (prompt-injection defence).
- Creating a proposal needs `WRITE` or `RUN_AGENT`. Deciding needs `WRITE`, and the decider must
  not be the proposer. `agent-runner` therefore can propose and can never decide.
- Accepting asserts the fact with `source_kind = proposer_kind`, `actor = proposer`,
  `proposal_id` set. Rejecting needs a reason. Both are audited.
- **The gate runs in shadow (principle 1).** On creation the pure gate records what it *would* do:
  ```
  gate(verdict, confidence, type_conflict, precedents):
      type_conflict                       -> hold
      any human precedent contradicts     -> hold
      threshold = 0.75 if any human precedent agrees else 0.85
      verdict decisive and confidence >= threshold -> auto
      otherwise                           -> hold
  ```
  Only decisions with a human actor count as precedent. The verdict is stored on the proposal as
  `gate = {decision, threshold, reasons}`. H1 never auto-applies; the agreement between `gate` and
  the human decision is the measurement that H2 needs before enforcing (with utopia's fuse: 2
  reverts of auto-applied decisions within 7 days switches enforcement off and raises an alert).

### 3.9 Reasoning and conflicts

`derive()` forward-chains over live `typed` facts held now:

| Flag on relation `p` | Rule |
|---|---|
| `symmetric` | `a p b → b p a` |
| `inverse_of = q` | `a p b → b q a` |
| `sub_property_of = q` | `a p b → a q b` |
| `transitive` | `a p b ∧ b p c → a p c` |

- Fixed point with a sorted frontier, max depth 12, at most 20,000 derived facts per predicate
  (exceeding it stops that predicate and reports it).
- Validity is the intersection of premise intervals (empty → no derivation). Confidence is the
  minimum.
- **An asserted fact beats a derived one:** a derived fact identical to an asserted one is not
  written; a derived value that contradicts an asserted value of a functional relation becomes a
  violation, never a fact.
- Each run rebuilds the wanted set: new derived facts are inserted with `layer = derived`,
  `source_kind = rule`, and a `world_derivation(rule, premise_ids)` proof; live derived facts not in
  the wanted set are invalidated with reason `underived`. Running twice changes nothing.

`conflicts()` is computed on read: two live values of a functional (or inverse-functional) relation
for the same subject (object) whose world intervals overlap, plus §3.6 rule 4 and reasoning
violations.

### 3.10 Decision ledger and drift (++)

Utopia lists decision replay as "in development" and has no code for it. Forge ships it in H1.

- `world_decision(title, outcome, actor, as_of, at, fact_ids[], links{})` is append-only. `links`
  carries forge identities: `spec_key`, `approval_id`, `workflow_run_id`, and A's changeset id
  once it lands.
- `drift(decision)` returns, for each fact it rested on: `unchanged`, `invalidated(reason)` or
  `superseded(by)`, plus `drifted = any(status != unchanged)`. Because facts are immutable, *what was
  believed* is exactly the referenced rows; drift is *what has changed since*.
- This is the hook K uses for continuous spec drift: a spec approval records a decision; a push
  that changes a rested-on fact shows the spec as drifted.

## 4. Phases

| Phase | Scope | Status |
|---|---|---|
| **H1** | Everything in §3 and §5–§10 | Implementation-ready |
| **H2** | Alignment and materialisation (phrase signatures → relations, two votes, fingerprinted staleness); identity channel 2 + context vectors + adjudicator with precedents; enforced governance + fuse; business rules (conditions + expression trees); relative time resolved against `doc_time`; OWL import and ontology packs (licences per ADR-0010) | Draft |
| **H3** | Actions with typed params, grants, preview and an execution gate wired to forge policy + approvals (utopia ADR 0034 proposed, unbuilt); scenarios (what-if on a forked `as_of`); chat with `[n]` citations and the two-axis system prompt; RDF + PROV-O export; ask-the-data (one SELECT, read-only session, 10 s timeout) | Draft |
| **H4** | Incremental reasoning and versioned proofs; parallel extraction with per-task model routing through forge's cost router; bitemporal full text and `at`-aware retrieval; connectors via existing PM adapters | Draft |

## 5. Components (H1)

### 5.1 `packages/world-model/forge_world/`

| Module | Contents | Pure? |
|---|---|---|
| `time.py` | `Precision`, `WorldBound`, `parse_bound(text)`, `bucket_end`, `holds_at`, `held_at` | yes |
| `ledger.py` | `Layer`, `Mood`, `SourceKind`, `RelationKind`, `Statement`, `normalise_phrase`, `dedup_key` | yes |
| `temporal.py` | `compute_ends(facts) -> (closures, conflicts)` | yes |
| `identity.py` | `normalise_name`, `strip_suffixes`, `names_match`, `types_compatible` | yes |
| `extraction.py` | `build_request`, `parse_reply`, `verify(chunk, doc_head, items) -> (accepted, drops)` | yes |
| `governance.py` | `Precedent`, `GateDecision`, `gate(...)` | yes |
| `reasoning.py` | `Axioms`, `derive(facts, relations) -> (derived, violations, capped)` | yes |
| `conflicts.py` | `functional_conflicts(facts, relations)` | yes |
| `decisions.py` | `drift(fact_rows) -> DriftReport` | yes |
| `repository.py` | `WorldRepository(session)`: the only SQL, including the two time predicates | no |
| `service.py` | `WorldModelService(repo, model_client, clock)`: orchestration, transactions | no |

`pyproject.toml` depends on `forge-contracts`, `forge-db`, `forge-knowledge` (chunker only),
`sqlalchemy`, `pydantic`. The package is added to the root `pyproject.toml`, `ruff.toml`,
`MYPY_PACKAGES`, `uv.lock` and `tests/test_versioning.py` (CONTRIBUTING rule).

### 5.2 `forge_contracts`

No changes. The service consumes `ModelClient`, `ModelRequest`, `ModelResponse` as they are.

### 5.3 Database — `packages/db/forge_db/models/world.py`, migration `0041_world_model.py`

All tables are `WorkspaceScopedModel` (UUID id, `workspace_id` CASCADE, `created_at`,
`updated_at`). `recorded_at` is `created_at`.

| Table | Key columns | Mutability |
|---|---|---|
| `world_class` | `key`, `label`, `parent_key`, `disjoint_with` (JSON), `iri`; unique `(workspace_id, key)` | editable, audited |
| `world_relation` | `key`, `label`, `kind`, `domain_class`, `range_class`, six boolean flags, `inverse_of`, `sub_property_of`, `iri`; unique `(workspace_id, key)` | editable, audited |
| `world_entity` | `label`, `type_key`, `type_source`, `described`, `described_in` | label/type editable, audited |
| `world_fact` | `subject_id`, `phrase`, `predicate_key`, `object_id` XOR `value` (CHECK), `layer`, `mood`, `qualifiers` (JSON), `confidence` (CHECK 0–1), `valid_from`, `valid_from_precision`, `valid_to`, `valid_to_precision`, `end_derived`, `end_after_period`, `source_kind`, `actor`, `proposal_id`, `supersedes_id`, `dedup_key`, `invalidated_at`, `invalidated_reason`; partial unique `(workspace_id, dedup_key) WHERE invalidated_at IS NULL`; indexes on subject, object, predicate | **tombstone-only trigger** |
| `world_evidence` | `fact_id`, `source_ref`, `chunk_index`, `quote`, `start_offset`, `end_offset`, `doc_time` | append-only trigger |
| `world_extraction_drop` | `source_ref`, `chunk_index`, `reason`, `item` (JSON) | append-only trigger |
| `world_proposal` | `kind`, `payload` (JSON), `status ∈ {proposed, accepted, rejected}`, `proposer_kind`, `proposer`, `rationale`, `gate` (JSON), `decided_by`, `decided_at`, `decision_reason`, `fact_id` | status transitions once |
| `world_entity_merge` | `survivor_id`, `merged_id`, `actor`, `reason`, `reverted_at`, `reverted_by`, `revert_reason`; partial unique `(workspace_id, merged_id) WHERE reverted_at IS NULL` | revert stamp once |
| `world_derivation` | `fact_id`, `rule`, `premise_ids` (JSON) | append-only trigger |
| `world_decision` | `title`, `outcome`, `actor`, `as_of`, `at`, `fact_ids` (JSON), `links` (JSON) | append-only trigger |

**Tombstone-only trigger** on `world_fact` (Postgres; the repository is the guard on SQLite):

```sql
IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'world_fact is append-only'; END IF;
IF OLD.invalidated_at IS NOT NULL THEN RAISE EXCEPTION 'world_fact % is already invalidated', OLD.id; END IF;
IF (to_jsonb(NEW) - 'invalidated_at' - 'invalidated_reason' - 'updated_at')
   IS DISTINCT FROM (to_jsonb(OLD) - 'invalidated_at' - 'invalidated_reason' - 'updated_at')
THEN RAISE EXCEPTION 'world_fact % may only be invalidated', OLD.id; END IF;
```

The append-only tables reuse `attach_immutability_trigger`. The migration follows 0038's
idempotent pattern (create only what is missing; drop only what it owns) and chains
`down_revision = "0040_self_eval_baseline"` (ADR-0009).

### 5.4 API — `apps/api/forge_api/routers/world.py`

All routes require an authenticated principal and scope by `principal.workspace_id`;
cross-workspace ids are `404`. Reads need `READ`. Times are ISO-8601 query parameters.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/world/entities?q=&type=&at=&as_of=&limit=&offset=` | READ | Search by name (`known_as` + label) |
| GET | `/world/entities/{id}?at=&as_of=` | READ | Profile: resolved id, names, facts holding at `at` as known at `as_of` |
| GET | `/world/entities/{id}/changes?since=&until=` | READ | Record-axis events: `asserted`, `invalidated(reason)` |
| GET | `/world/facts?subject=&predicate=&object=&layer=&at=&as_of=` | READ | Fact query with evidence |
| POST | `/world/facts` | WRITE | Assert a fact as a person |
| POST | `/world/facts/{id}/supersede` | WRITE | Correct a fact |
| POST | `/world/facts/{id}/retract` | WRITE | Invalidate with reason `retracted` |
| POST | `/world/ingest` | WRITE | Extract facts from a document (503 when no model client is configured — never a fake) |
| GET | `/world/proposals?status=` | READ | Review queue |
| POST | `/world/proposals` | WRITE or RUN_AGENT | Propose a fact |
| POST | `/world/proposals/{id}/decision` | WRITE, not the proposer | Accept or reject |
| POST | `/world/merges` / `/world/merges/{id}/revert` | WRITE | Merge / revert entities |
| POST | `/world/derive` | WRITE | Run reasoning; returns counts, capped predicates, violations |
| GET | `/world/conflicts?at=` | READ | Functional conflicts + unordered successions + violations |
| GET/POST | `/world/ontology/classes`, `/world/ontology/relations` | READ / WRITE | Minimal ontology |
| POST | `/world/decisions` | WRITE or RUN_AGENT | Record a decision and the facts it rested on |
| GET | `/world/decisions/{id}` and `/world/decisions/{id}/drift` | READ | Decision and drift report |

Every mutation emits one audit event through the existing audit store (category `world`, action
`world.<verb>`), with the actor, workspace and ids — never fact text beyond 200 characters.

## 6. Data flow

```
document ──► chunker ──► ModelClient (temp 0) ──► verify ──► facts + evidence ──► temporal close
                                                     └──► drops (rows)
agent / MCP ──► proposal ──► shadow gate ──► person decides ──► fact (proposal_id)
person ──► assert / supersede / retract / merge / revert ──► ledger (+ audit)
derive ──► wanted set ──► insert derived (+ proof) / invalidate underived
read(at, as_of) ──► resolve merges ──► holds_at ∧ held_at ──► facts + evidence
decision(fact_ids, as_of) ──► drift = their status now
```

## 7. Error handling

| Case | Behaviour |
|---|---|
| No model client configured | `/world/ingest` returns 503 with the reason. Never a scripted fallback ("never fake a capability"). |
| Model call fails for a chunk | Drop row `chunk_unextracted`; other chunks continue; the response counts it. |
| Duplicate live fact | Return the existing fact (`created = false`). |
| Update on an invalidated fact | 409. |
| Merge of disjoint classes / self / already merged | 409 with the reason. |
| Proposal decided twice | 409. Decider is proposer → 403. |
| Reasoning cap hit | Predicate listed in `capped`; other predicates complete. |
| State span with equal ends; unknown relation key; key regex fails | 422. |

## 8. Testing

- **Pure modules** (SQLite-free, fast): table-driven tests for every rule in §3 — precision
  buckets, `holds_at`/`held_at` edges, dedup key stability, temporal closing order-independence
  (property: any permutation gives the same ends), every drop reason, suffix stripping and type
  compatibility, every gate branch, each axiom, interval intersection, the asserted-beats-derived
  rule, the depth and cap limits, drift statuses.
- **Repository and service on SQLite** (existing unit lane): append-only behaviour through the
  repository, dedup returning the existing row, supersede order, merge resolution and revert on both
  clocks, proposal lifecycle and the proposer≠decider rule, derive idempotence (second run is a
  no-op), ingest with a scripted `ModelClient` from `forge_agent.testing`.
- **Postgres lane** (`pg_engine` fixture; skipped with a PARKED reason when no Postgres): the
  tombstone trigger refuses updates other than the stamp and refuses a second stamp; the partial
  unique index rejects a concurrent duplicate; the migration upgrades and downgrades cleanly.
- **API**: each route's happy path, 404 across workspaces, 403 for viewer writes and for the
  proposer deciding, 503 for ingest without a model.

## 9. Acceptance criteria (H1)

1. Asserting the same fact twice yields one live row; the Postgres unique index holds under two
   concurrent sessions.
2. `GET /world/facts` with `as_of` before a correction returns the old value; after, the new one;
   `at` selects by world time independently.
3. Ingesting a document with a scripted model writes only statements whose quote is verbatim in the
   chunk; every rejected item is a drop row with its reason; offsets are computed by the server.
4. A functional state relation receiving a later value closes the earlier one with
   `end_derived = true`, independent of insertion order; an unknown-start pair is reported, not
   closed.
5. `agent-runner` can create a proposal and cannot decide one; a member cannot decide their own;
   accepting writes a fact carrying `proposal_id`; every proposal carries the shadow gate verdict.
6. `derive` writes symmetric, inverse, sub-property and transitive consequences with proofs, never
   overrides an asserted fact, and a second run changes nothing.
7. A decision recorded on three facts reports `drifted = true` after one of them is superseded,
   naming the successor.
8. Updating any `world_fact` column other than the stamp raises on Postgres.
9. `make lint`, `make typecheck` and `make test` pass with the new package registered.

## 10. What H1 deliberately does not do

- Call a model for anything except extraction.
- Auto-apply any proposal (shadow gate only).
- Import OWL or ship ontology packs (H2; schema.org is CC BY-SA, see ADR-0010).
- Provide a UI. The web surface is J's (`/knowledge` world tab) once the API is stable.
- Copy utopia prompt text, code, or documentation (ADR-0010). The extraction prompt in
  `forge_world/extraction.py` is written from the contract in §3.7.
