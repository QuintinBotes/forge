import { describe, expect, it } from "vitest";

import { hasMarkdownErrors, parseSpecMarkdown } from "./markdown-parse";

const VALID = `---
id: SPEC-1
status: draft
constitution_refs: []
repos: []
execution_mode: single_agent
skill_profile: null
plan_ref: null
tasks_ref: null
validation_ref: null
---

## Goal

Passwordless auth

## Requirements

- **R1**: Users can sign in without a password

## Acceptance Criteria

- **AC1** (R1): Given a valid magic link, when clicked, then the user is signed in

## Constraints

- Must work offline

## Open Questions

- **Q1**: Should magic links expire?
  - Resolution: After 15 minutes

## Decisions

### ADR-1 — Use signed tokens

- Status: accepted
- Context: Need a stateless link
- Decision: Sign the token
- Consequences: Requires a secret key
`;

describe("parseSpecMarkdown", () => {
  it("parses a fully valid spec.md with no issues", () => {
    const { manifest, issues } = parseSpecMarkdown(VALID);
    expect(issues).toEqual([]);
    expect(manifest.id).toBe("SPEC-1");
    expect(manifest.name).toBe("Passwordless auth");
    expect(manifest.requirements).toEqual([{ id: "R1", text: "Users can sign in without a password" }]);
    expect(manifest.acceptance_criteria).toEqual([
      {
        id: "AC1",
        text: "Given a valid magic link, when clicked, then the user is signed in",
        req_refs: ["R1"],
        spec_ref: null,
      },
    ]);
    expect(manifest.constraints).toEqual(["Must work offline"]);
    expect(manifest.open_questions).toEqual([
      { id: "Q1", text: "Should magic links expire?", resolution: "After 15 minutes" },
    ]);
    expect(manifest.decisions).toEqual([
      {
        id: "ADR-1",
        title: "Use signed tokens",
        status: "accepted",
        context: "Need a stateless link",
        decision: "Sign the token",
        consequences: "Requires a secret key",
      },
    ]);
    expect(hasMarkdownErrors(issues)).toBe(false);
  });

  it("requires a leading '---' frontmatter block", () => {
    const { issues } = parseSpecMarkdown("## Goal\n\nSomething\n");
    expect(hasMarkdownErrors(issues)).toBe(true);
    expect(issues[0].line).toBe(1);
    expect(issues[0].message).toMatch(/frontmatter/i);
  });

  it("flags an unterminated frontmatter block", () => {
    const { issues } = parseSpecMarkdown("---\nid: SPEC-1\n");
    expect(hasMarkdownErrors(issues)).toBe(true);
    expect(issues.some((i) => /unterminated/i.test(i.message))).toBe(true);
  });

  it("requires the 'id' frontmatter key", () => {
    const { issues } = parseSpecMarkdown("---\nstatus: draft\n---\n\n## Goal\n\nX\n");
    expect(issues.some((i) => /'id'/.test(i.message))).toBe(true);
  });

  it("requires a '## Goal' section", () => {
    const { issues } = parseSpecMarkdown("---\nid: SPEC-1\n---\n\n## Requirements\n\n- **R1**: text\n");
    expect(issues.some((i) => /Goal/.test(i.message))).toBe(true);
  });

  it("line-anchors a malformed requirement bullet", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Requirements\n\n- not a bullet\n";
    const { issues } = parseSpecMarkdown(text);
    const issue = issues.find((i) => /requirement must be/.test(i.message));
    expect(issue).toBeDefined();
    expect(issue?.line).toBe(text.split("\n").findIndex((l) => l === "- not a bullet") + 1);
  });

  it("line-anchors a malformed acceptance criterion bullet", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Acceptance Criteria\n\nnope\n";
    const { issues } = parseSpecMarkdown(text);
    expect(issues.some((i) => /acceptance criterion must be/.test(i.message))).toBe(true);
  });

  it("folds 2-space continuation lines into a multi-line checklist criterion", () => {
    const text =
      "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Acceptance Criteria\n\n" +
      "- **AC1** (R1): - [ ] Email validates\n  - [x] Password masked\n";
    const { manifest, issues } = parseSpecMarkdown(text);
    expect(hasMarkdownErrors(issues)).toBe(false);
    expect(manifest.acceptance_criteria).toEqual([
      { id: "AC1", text: "- [ ] Email validates\n- [x] Password masked", req_refs: ["R1"], spec_ref: null },
    ]);
  });

  it("flags an acceptance continuation line before any criterion", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Acceptance Criteria\n\n  - [ ] orphan\n";
    const { issues } = parseSpecMarkdown(text);
    expect(issues.some((i) => /continuation line before any criterion/.test(i.message))).toBe(true);
  });

  it("flags a resolution with no preceding open question", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Open Questions\n\n  - Resolution: orphan\n";
    const { issues } = parseSpecMarkdown(text);
    expect(issues.some((i) => /no preceding open question/.test(i.message))).toBe(true);
  });

  it("flags an unknown section as a warning, not an error", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Nonsense\n\nsomething\n";
    const { issues, manifest } = parseSpecMarkdown(text);
    const issue = issues.find((i) => /unknown section/.test(i.message));
    expect(issue?.severity).toBe("warning");
    expect(hasMarkdownErrors(issues)).toBe(false);
    expect(manifest.name).toBe("X");
  });

  it("parses a decision heading and rejects a malformed one", () => {
    const text =
      "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Decisions\n\n### ADR-1 no separator\n\n- Status: accepted\n";
    const { issues } = parseSpecMarkdown(text);
    expect(issues.some((i) => /decision heading must be/.test(i.message))).toBe(true);
  });

  it("is best-effort: still returns a manifest alongside issues", () => {
    const { manifest, issues } = parseSpecMarkdown("nonsense");
    expect(hasMarkdownErrors(issues)).toBe(true);
    expect(manifest.id).toBe("");
    expect(manifest.requirements).toEqual([]);
  });
});
