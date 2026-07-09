import { describe, expect, it } from "vitest";

import { hasErrors, validateManifestYaml } from "./yaml-schema";

const VALID_MANIFEST = `id: SPEC-1
name: Passwordless auth
status: draft
constitution_refs: []
repos: []
requirements:
  - id: R1
    text: Users can sign in without a password
acceptance_criteria:
  - id: AC1
    text: Given a valid magic link, when clicked, then the user is signed in
    req_refs: [R1]
constraints: []
open_questions: []
decisions: []
execution_mode: single_agent
skill_profile: null
plan_ref: null
tasks_ref: null
validation_ref: null
`;

describe("validateManifestYaml", () => {
  it("has no issues for a fully valid manifest", () => {
    expect(validateManifestYaml(VALID_MANIFEST)).toEqual([]);
  });

  it("flags an empty document", () => {
    const issues = validateManifestYaml("   \n");
    expect(hasErrors(issues)).toBe(true);
    expect(issues[0].message).toMatch(/empty/i);
  });

  it("line-anchors a YAML syntax error", () => {
    const text = `id: SPEC-1\nname: [unterminated\n`;
    const issues = validateManifestYaml(text);
    expect(hasErrors(issues)).toBe(true);
    expect(issues[0].line).toBeGreaterThanOrEqual(2);
  });

  it("requires 'id' and 'name'", () => {
    const issues = validateManifestYaml("status: draft\n");
    const messages = issues.map((i) => i.message);
    expect(messages.some((m) => /'id'/.test(m))).toBe(true);
    expect(messages.some((m) => /'name'/.test(m))).toBe(true);
  });

  it("rejects an invalid status value with a line number", () => {
    const text = `id: SPEC-1\nname: X\nstatus: not-a-status\n`;
    const issues = validateManifestYaml(text);
    const statusIssue = issues.find((i) => /status/.test(i.message));
    expect(statusIssue).toBeDefined();
    expect(statusIssue?.line).toBe(3);
  });

  it("rejects an invalid execution_mode", () => {
    const text = `id: SPEC-1\nname: X\nexecution_mode: yolo\n`;
    const issues = validateManifestYaml(text);
    expect(issues.some((i) => /execution_mode/.test(i.message))).toBe(true);
  });

  it("flags a requirement missing 'text' with a line-anchored error", () => {
    const text = `id: SPEC-1\nname: X\nrequirements:\n  - id: R1\n`;
    const issues = validateManifestYaml(text);
    const issue = issues.find((i) => /requirements\[0\]/.test(i.message));
    expect(issue).toBeDefined();
    expect(issue?.line).toBe(4);
  });

  it("flags requirements that isn't a list", () => {
    const text = `id: SPEC-1\nname: X\nrequirements: not-a-list\n`;
    const issues = validateManifestYaml(text);
    expect(issues.some((i) => /'requirements' must be a list/.test(i.message))).toBe(true);
  });

  it("warns when an acceptance criterion references an unknown requirement", () => {
    const text = `id: SPEC-1\nname: X\nrequirements:\n  - id: R1\n    text: A\nacceptance_criteria:\n  - id: AC1\n    text: B\n    req_refs: [R9]\n`;
    const issues = validateManifestYaml(text);
    const warning = issues.find((i) => /unknown requirement/.test(i.message));
    expect(warning).toBeDefined();
    expect(warning?.severity).toBe("warning");
  });

  it("flags a non-string entry in a string-array field", () => {
    const text = `id: SPEC-1\nname: X\nconstraints:\n  - 42\n`;
    const issues = validateManifestYaml(text);
    expect(issues.some((i) => /constraints\[0\]/.test(i.message))).toBe(true);
  });
});
