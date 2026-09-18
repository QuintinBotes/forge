/**
 * Client-side, best-effort port of `forge_spec.markdown.parse_spec_md` for
 * Spec Studio's Markdown mode live preview.
 *
 * `spec.md` is one of the two first-class, round-tripping serializations of a
 * `SpecManifest` (the other being `manifest.yaml`; see
 * `forge_spec.markdown`/`forge_spec.manifest` on the backend). This module
 * gives the Markdown editor a *fast, offline* structural parse — with
 * line-anchored errors mirroring the backend's `SpecParseError` — so the
 * Structure/Preview/Traceability panes and error list update as the user
 * types. Unlike the backend parser (which raises on the first problem), this
 * collects *every* issue it can find and always returns a best-effort
 * `SpecManifest` (defaulting fields it couldn't parse) so the panes stay
 * useful mid-edit. `PUT /spec/specs/{id}/markdown` remains the authoritative
 * parser — this never replaces it.
 */

import { parse as parseYaml } from "yaml";

import type { ADR, AcceptanceCriterion, OpenQuestion, Requirement, SpecManifest, SpecStatus } from "@/lib/api/types";

export type MarkdownIssueSeverity = "error" | "warning";

export interface MarkdownIssue {
  /** 1-indexed line number the issue anchors to. */
  line: number;
  message: string;
  severity: MarkdownIssueSeverity;
}

export interface ParsedSpecMarkdown {
  /** Best-effort manifest — always populated, even alongside issues. */
  manifest: SpecManifest;
  issues: MarkdownIssue[];
}

const H2 = "## ";
const H3 = "### ";
const ADR_SEP = " — ";

const BOLD_BULLET = /^- \*\*([^*]+)\*\*:\s?(.*)$/;
const ACCEPT_BULLET = /^- \*\*([^*]+)\*\*(?: \(([^)]*)\))?:\s?(.*)$/;
const RESOLUTION = /^ {2}- Resolution:\s?(.*)$/;
const ADR_FIELD = /^- (Status|Context|Decision|Consequences):\s?(.*)$/;

const ADR_FIELD_ATTR: Record<string, "status" | "context" | "decision" | "consequences"> = {
  Status: "status",
  Context: "context",
  Decision: "decision",
  Consequences: "consequences",
};

interface Section {
  title: string;
  headerLine: number;
  lines: [number, string][];
}

function nonBlank(section: Section): [number, string][] {
  return section.lines.filter(([, text]) => text.trim() !== "");
}

function splitFrontmatter(
  lines: string[],
  issues: MarkdownIssue[],
): { data: Record<string, unknown>; bodyStart: number } {
  let idx = 0;
  const n = lines.length;
  while (idx < n && lines[idx].trim() === "") idx += 1;
  if (idx >= n || lines[idx].trim() !== "---") {
    issues.push({
      line: idx + 1,
      message: "spec.md must begin with a '---' YAML frontmatter block",
      severity: "error",
    });
    return { data: {}, bodyStart: n };
  }
  const openLine = idx + 1;
  idx += 1;
  const fmBody: string[] = [];
  while (idx < n && lines[idx].trim() !== "---") {
    fmBody.push(lines[idx]);
    idx += 1;
  }
  if (idx >= n) {
    issues.push({ line: openLine, message: "unterminated frontmatter: missing closing '---'", severity: "error" });
    return { data: {}, bodyStart: n };
  }
  try {
    const parsed = parseYaml(fmBody.join("\n"));
    if (parsed == null) return { data: {}, bodyStart: idx + 1 };
    if (typeof parsed !== "object" || Array.isArray(parsed)) {
      issues.push({ line: openLine + 1, message: "frontmatter must be a YAML mapping", severity: "error" });
      return { data: {}, bodyStart: idx + 1 };
    }
    return { data: parsed as Record<string, unknown>, bodyStart: idx + 1 };
  } catch (error) {
    issues.push({
      line: openLine + 1,
      message: `invalid YAML frontmatter: ${error instanceof Error ? error.message : String(error)}`,
      severity: "error",
    });
    return { data: {}, bodyStart: idx + 1 };
  }
}

function collectSections(lines: string[], start: number, issues: MarkdownIssue[]): Section[] {
  const sections: Section[] = [];
  let current: Section | null = null;
  for (let offset = start; offset < lines.length; offset += 1) {
    const raw = lines[offset];
    const lineNo = offset + 1;
    if (raw.startsWith(H2)) {
      current = { title: raw.slice(H2.length).trim(), headerLine: lineNo, lines: [] };
      sections.push(current);
      continue;
    }
    if (current === null) {
      if (raw.trim() === "") continue;
      issues.push({ line: lineNo, message: "unexpected content before first '##' section", severity: "error" });
      continue;
    }
    current.lines.push([lineNo, raw]);
  }
  return sections;
}

function parseGoal(section: Section, issues: MarkdownIssue[]): string {
  const body = section.lines
    .map(([, text]) => text)
    .join("\n")
    .trim();
  if (!body) {
    issues.push({ line: section.headerLine, message: "## Goal section is empty", severity: "error" });
  }
  return body;
}

function parseRequirements(section: Section, issues: MarkdownIssue[]): Requirement[] {
  const out: Requirement[] = [];
  for (const [lineNo, text] of nonBlank(section)) {
    const match = BOLD_BULLET.exec(text);
    if (!match) {
      issues.push({ line: lineNo, message: "requirement must be '- **ID**: text'", severity: "error" });
      continue;
    }
    out.push({ id: match[1].trim(), text: match[2].trim() });
  }
  return out;
}

function parseRefs(refs: string | undefined): { reqRefs: string[]; specRef: string | null } {
  if (refs === undefined) return { reqRefs: [], specRef: null };
  let reqRefs: string[] = [];
  let specRef: string | null = null;
  for (const part of refs.split(";")) {
    const chunk = part.trim();
    if (!chunk) continue;
    if (chunk.startsWith("spec=")) {
      specRef = chunk.slice("spec=".length).trim() || null;
    } else {
      reqRefs = chunk
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean);
    }
  }
  return { reqRefs, specRef };
}

function parseAcceptance(section: Section, issues: MarkdownIssue[]): AcceptanceCriterion[] {
  const out: AcceptanceCriterion[] = [];
  for (const [lineNo, text] of nonBlank(section)) {
    if (text.startsWith("  ")) {
      // 2-space continuation line — folds into the preceding criterion's text
      // (e.g. a multi-line checklist criterion's `- [ ] item` entries).
      if (out.length === 0) {
        issues.push({ line: lineNo, message: "acceptance continuation line before any criterion", severity: "error" });
        continue;
      }
      const prev = out[out.length - 1];
      out[out.length - 1] = { ...prev, text: `${prev.text}\n${text.slice(2)}` };
      continue;
    }
    const match = ACCEPT_BULLET.exec(text);
    if (!match) {
      issues.push({
        line: lineNo,
        message: "acceptance criterion must be '- **ID** (refs): text'",
        severity: "error",
      });
      continue;
    }
    const { reqRefs, specRef } = parseRefs(match[2]);
    out.push({ id: match[1].trim(), text: match[3].trim(), req_refs: reqRefs, spec_ref: specRef });
  }
  return out;
}

function parseConstraints(section: Section, issues: MarkdownIssue[]): string[] {
  const out: string[] = [];
  for (const [lineNo, text] of nonBlank(section)) {
    if (!text.startsWith("- ")) {
      issues.push({ line: lineNo, message: "constraint must be a '- ' bullet", severity: "error" });
      continue;
    }
    out.push(text.slice(2).trim());
  }
  return out;
}

function parseOpenQuestions(section: Section, issues: MarkdownIssue[]): OpenQuestion[] {
  const out: OpenQuestion[] = [];
  for (const [lineNo, text] of nonBlank(section)) {
    const resolution = RESOLUTION.exec(text);
    if (resolution) {
      if (out.length === 0) {
        issues.push({ line: lineNo, message: "resolution has no preceding open question", severity: "error" });
        continue;
      }
      out[out.length - 1] = { ...out[out.length - 1], resolution: resolution[1].trim() };
      continue;
    }
    const match = BOLD_BULLET.exec(text);
    if (!match) {
      issues.push({ line: lineNo, message: "open question must be '- **ID**: text'", severity: "error" });
      continue;
    }
    out.push({ id: match[1].trim(), text: match[2].trim() });
  }
  return out;
}

function parseDecisions(section: Section, issues: MarkdownIssue[]): ADR[] {
  const out: ADR[] = [];
  let fields: Record<string, string> = {};
  let header: { id: string; title: string } | null = null;

  const flush = () => {
    if (header === null) return;
    out.push({ id: header.id, title: header.title, ...fields } as ADR);
    fields = {};
    header = null;
  };

  for (const [lineNo, text] of nonBlank(section)) {
    if (text.startsWith(H3)) {
      flush();
      const body = text.slice(H3.length);
      if (!body.includes(ADR_SEP)) {
        issues.push({ line: lineNo, message: "decision heading must be '### ID — Title'", severity: "error" });
        continue;
      }
      const sepIdx = body.indexOf(ADR_SEP);
      header = { id: body.slice(0, sepIdx).trim(), title: body.slice(sepIdx + ADR_SEP.length).trim() };
      continue;
    }
    const field = ADR_FIELD.exec(text);
    if (!field) {
      issues.push({
        line: lineNo,
        message: "decision field must be '- Status|Context|Decision|Consequences: text'",
        severity: "error",
      });
      continue;
    }
    if (header === null) {
      issues.push({ line: lineNo, message: "decision field before any '### ID — Title'", severity: "error" });
      continue;
    }
    fields[ADR_FIELD_ATTR[field[1]]] = field[2].trim();
  }
  flush();
  return out;
}

const FRONTMATTER_STRING_ARRAY_FIELDS = ["constitution_refs", "repos"] as const;
const FRONTMATTER_NULLABLE_STRING_FIELDS = ["review_note", "plan_ref", "tasks_ref", "validation_ref", "skill_profile"] as const;

/**
 * Parse `spec.md` `text` into a best-effort `SpecManifest` plus every
 * line-anchored issue found. Never throws — a malformed document still
 * yields a (possibly empty) manifest so callers can keep rendering.
 */
export function parseSpecMarkdown(text: string): ParsedSpecMarkdown {
  const issues: MarkdownIssue[] = [];
  const lines = text.split("\n");
  const { data, bodyStart } = splitFrontmatter(lines, issues);

  const id = typeof data.id === "string" ? data.id : "";
  if (!("id" in data)) {
    issues.push({ line: 1, message: "frontmatter is missing required key 'id'", severity: "error" });
  }

  let name: string | null = null;
  let requirements: Requirement[] = [];
  let acceptanceCriteria: AcceptanceCriterion[] = [];
  let constraints: string[] = [];
  let openQuestions: OpenQuestion[] = [];
  let decisions: ADR[] = [];

  for (const section of collectSections(lines, bodyStart, issues)) {
    switch (section.title) {
      case "Goal":
        name = parseGoal(section, issues);
        break;
      case "Requirements":
        requirements = parseRequirements(section, issues);
        break;
      case "Acceptance Criteria":
        acceptanceCriteria = parseAcceptance(section, issues);
        break;
      case "Constraints":
        constraints = parseConstraints(section, issues);
        break;
      case "Open Questions":
        openQuestions = parseOpenQuestions(section, issues);
        break;
      case "Decisions":
        decisions = parseDecisions(section, issues);
        break;
      default:
        issues.push({
          line: section.headerLine,
          message: `unknown section '## ${section.title}'`,
          severity: "warning",
        });
    }
  }

  if (name === null) {
    issues.push({ line: 1, message: "spec.md is missing a '## Goal' section (the spec name)", severity: "error" });
  }

  const manifest: SpecManifest = {
    id,
    name: name ?? "",
    requirements,
    acceptance_criteria: acceptanceCriteria,
    constraints,
    open_questions: openQuestions,
    decisions,
  };
  if (typeof data.status === "string") manifest.status = data.status as SpecStatus;
  if (typeof data.execution_mode === "string") {
    manifest.execution_mode = data.execution_mode as SpecManifest["execution_mode"];
  }
  for (const field of FRONTMATTER_STRING_ARRAY_FIELDS) {
    if (Array.isArray(data[field])) manifest[field] = data[field] as string[];
  }
  for (const field of FRONTMATTER_NULLABLE_STRING_FIELDS) {
    if (typeof data[field] === "string" || data[field] === null) {
      manifest[field] = data[field] as string | null;
    }
  }

  return { manifest, issues };
}

export function hasMarkdownErrors(issues: MarkdownIssue[]): boolean {
  return issues.some((issue) => issue.severity === "error");
}
