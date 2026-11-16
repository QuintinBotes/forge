# Swarm Code Factory Constitution

SCF is a software factory: it takes an issue and ships a verified, attested change. These
principles bind every Spec Kit spec, plan and task list under `specs/`. Each one names the
repository document it comes from and a test a reviewer can apply.

## Core Principles

### I. Verifiable delivery comes before agent capability

SCF competes on proof that a change is correct, not on a better coding agent. A feature that
changes how work is produced MUST also state how the result is verified and what evidence is kept.

- Test: every spec under `specs/` names the check, gate or evidence record that proves its outcome.
  A spec whose only claim is "the agent does more" fails review.
- Source: `docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md` section 1 (thesis);
  `docs/SCF_SPEC.md` Core Design Principles 2 and 10.

### II. Agent harnesses and models are swappable

SCF does not build a harness and does not depend on one model vendor. Model access goes through
the `ModelClient` contract in `packages/contracts`, and executors are selected by configuration.

- Test: no requirement names a single harness or model vendor as mandatory. A change that works
  with only one provider must say so and must refuse, not degrade silently, on the others.
- Source: roadmap section 1, point 1; `docs/SCF_SPEC.md` Core Design Principle 5 (bring your own
  model and key); `docs/architecture/adr/0012-two-executor-modes.md`.

### III. Every agent change is independently verified and attested (NON-NEGOTIABLE)

The agent that writes a change does not grade it. Checks are re-run outside the agent, a reviewer
other than the author decides, and the result carries a signed attestation chained into the audit
log. A person approves before a change lands unless a recorded risk policy says otherwise.

- Test: for any path on which an agent can produce a change, a test shows that the change cannot
  reach an approved state without an independent check result and an attestation record.
- Source: roadmap section 1, point 2; `docs/trust-layer.md` (Attested Changesets, Red-Team Gate,
  Self-Eval Gate); `docs/architecture/adr/0005-machines-propose-people-decide.md`.

### IV. Tests are never weakened to pass (NON-NEGOTIABLE)

New behaviour ships with a test that fails before the change. No test is deleted, skipped,
deselected, marked `xfail` or loosened to make a check pass. A test that is wrong is fixed in a
change that says why.

- Test: a diff that removes an assertion, adds a skip marker or lowers a threshold carries a written
  justification that is independent of making the build green. Without one the review rejects it.
- Source: `CONTRIBUTING.md` ("Tests, linting, and the green gate");
  `docs/superpowers/plans/orbit-runs/README.md` and the BOUNDARIES block of each goal file there.

### V. Production paths fail closed and never fake a capability

An unconfigured dependency refuses with a reason. Offline, scripted or fake behaviour runs only in
tests or behind a named development flag. A gap is reported as a gap, not as a pass.

- Test: each new capability has a test for its unconfigured state that asserts a refusal (an HTTP
  error, a failed activity or a failed run). `tests/test_no_fake_capabilities.py` keeps passing.
- Source: `docs/architecture/adr/0007-production-paths-fail-closed.md`;
  `docs/architecture/adr/0018-retrieval-degrades-honestly.md`.

### VI. Agents act only through a gated tool belt

Agents run in a sandbox, call only the tools their role allows, and stop when their budget is
spent. Content an agent reads is untrusted. Writing outside the run's worktree, opening pull
requests, approving and asserting shared facts are done by finalizers or by people, never by a tool
an agent can call during a run. Machine writes to shared knowledge are proposals.

- Test: a new agent tool is registered through `ROLE_TOOLS`, is checked by the policy gate on every
  call, and has a test that a role without the tool cannot invoke it.
- Source: `docs/architecture/adr/0008-agents-act-through-a-gated-tool-belt.md`;
  `docs/architecture/adr/0014-untrusted-content-cannot-drive-privileged-tools.md`;
  `docs/architecture/adr/0005-machines-propose-people-decide.md`.

### VII. Public-repository hygiene

This repository is public. No secret, token, personal path, username or email is committed.
Vulnerabilities are reported privately and referred to by finding ID, without exploitation detail.
Code, prompt text and documentation from other projects are not copied without the licence notices
ADR-0010 requires. Private sibling repositories are described only by what SCF's public documents
already say.

- Test: the `gitleaks` gate in CI passes; file paths in documents are repository-relative; a
  security reference is an ID from `SECURITY_FINDINGS.md` or `docs/security/pentest-punch-list.md`.
- Source: `CONTRIBUTING.md` ("Before opening a PR", "Reporting bugs and security issues");
  `SECURITY.md`; `docs/architecture/adr/0010-licence-and-clean-room-policy.md`.

### VIII. Conventional Commits with DCO sign-off

Every commit subject follows Conventional Commits (`type(scope): summary`) and every commit is
signed off. The changelog and the version bump are derived from the history, so the format is
enforced.

- Test: `uv run cz check` passes over the commit range, and each commit carries a `Signed-off-by`
  trailer.
- Source: `CONTRIBUTING.md` ("Commit conventions", "Developer Certificate of Origin").

## Specification Layout

Three kinds of spec live in or around this repository. They do not replace each other.

- **Spec Kit specs** for new SCF work live under `specs/NNN-slug/` (`spec.md`, then `plan.md` and
  `tasks.md`). They follow the templates in `.specify/templates/`.
- **Lettered design specs** (A to M) stay in `docs/superpowers/specs/`, with their decisions in
  `docs/architecture/adr/`. They are design input. A Spec Kit spec cites the lettered spec and ADRs
  it builds on. Lettered specs are not migrated to the Spec Kit layout.
- **Runtime specs** are what SCF's users author through SCF's own spec engine. That engine uses
  a different layout, `specs/SPEC-<n>-<slug>/` with a `manifest.yaml`, described in
  `docs/SCF_SPEC.md` ("Spec Folder Layout") and shown in `examples/specs/`. It is a product
  feature and is not the format for SCF's own roadmap work.

The directory prefix tells the two `specs/` layouts apart: three digits for Spec Kit, `SPEC-` for
the runtime engine.

## Development Workflow

- Trunk-based development against `main`, short-lived branches, one focused change per pull request
  (`CONTRIBUTING.md`, "Development workflow").
- Features and cross-cutting work get a Spec Kit spec. Small fixes and documentation get a plain
  issue (roadmap section 3, decision D4).
- Every acceptance criterion names the check that proves it. A criterion no check can prove is
  marked for human review.
- Migration numbers are assigned when a change lands, not reserved in a spec
  (`docs/architecture/adr/0009-migration-numbers-are-assigned-at-merge.md`).
- Interoperability standards are pinned to a version in code and documents
  (`docs/architecture/adr/0016-interoperability-standards-are-pinned.md`).

## Governance

This constitution takes precedence over a spec, plan or task list that conflicts with it. An
accepted ADR takes precedence over this file on the decision it records; if the two disagree, the
constitution is amended in the same change that notes the ADR.

An amendment is a pull request that edits this file, states the reason, updates the version below
and names any spec that must change as a result. Version numbers follow semantic versioning: a
removed or redefined principle is a major change, a new principle or section is a minor change, a
clarification is a patch.

Reviewers check each spec and each change against the principles above. A deviation is written
down in the spec or the pull request with its reason, or the change is not accepted.

**Version**: 1.0.0 | **Ratified**: 2026-11-14 | **Last Amended**: 2026-11-14
