/**
 * Schema-aware validation for the Spec Studio YAML manifest mode.
 *
 * `manifest.yaml` is one of the two first-class, round-tripping
 * serializations of a `SpecManifest` (the other being `spec.md`; see
 * `forge_spec.markdown`/`forge_spec.manifest` on the backend). This module
 * gives the YAML editor *client-side* structural + shape validation with
 * line-anchored errors, so a mistyped or malformed manifest is caught before
 * it ever reaches `PUT /spec/specs/{id}/manifest` — the backend
 * (`forge_spec.FileSpecEngine.save_manifest_yaml`) remains the authoritative
 * parser; this is a fast, offline first pass mirroring its shape.
 */

import { isMap, isPair, isScalar, isSeq, parseDocument, type Document, type ParsedNode } from "yaml";

import { SPEC_STATUSES, type SpecStatus } from "@/lib/api/types";

export type YamlIssueSeverity = "error" | "warning";

export interface YamlIssue {
  /** 1-indexed line number the issue anchors to. */
  line: number;
  /** 1-indexed column, when known. */
  column?: number;
  message: string;
  severity: YamlIssueSeverity;
}

const EXECUTION_MODES = ["single_agent", "supervised_multi_agent"] as const;

const STRING_ARRAY_FIELDS = ["constitution_refs", "repos", "constraints"] as const;
const NULLABLE_STRING_FIELDS = ["plan_ref", "tasks_ref", "validation_ref", "skill_profile"] as const;

/** Required scalar id/text shape shared by requirements, ACs, questions, ADRs. */
function offsetToLine(text: string, offset: number): { line: number; column: number } {
  let line = 1;
  let lastNewline = -1;
  for (let i = 0; i < offset && i < text.length; i += 1) {
    if (text[i] === "\n") {
      line += 1;
      lastNewline = i;
    }
  }
  return { line, column: offset - lastNewline };
}

function nodeStart(node: ParsedNode | null | undefined): number | undefined {
  return node?.range ? node.range[0] : undefined;
}

/** Resolve the item at `path` (dot/bracket-free, `[key, index, key]`) within the YAML AST. */
function resolveNode(doc: Document.Parsed, path: (string | number)[]): ParsedNode | null {
  const node = doc.getIn(path, true);
  if (isPair(node)) {
    return (node.value as ParsedNode | null) ?? (node.key as ParsedNode | null);
  }
  return (node as ParsedNode | null) ?? null;
}

function pushIssue(
  issues: YamlIssue[],
  text: string,
  node: ParsedNode | null | undefined,
  message: string,
  severity: YamlIssueSeverity = "error",
): void {
  const offset = nodeStart(node);
  const { line, column } = offset !== undefined ? offsetToLine(text, offset) : { line: 1, column: 1 };
  issues.push({ line, column, message, severity });
}

function checkStringList(
  doc: Document.Parsed,
  text: string,
  key: string,
  issues: YamlIssue[],
): void {
  const node = resolveNode(doc, [key]);
  if (node == null) return;
  if (!isSeq(node)) {
    pushIssue(issues, text, node, `'${key}' must be a list of strings`);
    return;
  }
  node.items.forEach((item, index) => {
    const itemNode = item as ParsedNode;
    if (!isScalar(itemNode) || typeof itemNode.value !== "string") {
      pushIssue(issues, text, itemNode, `${key}[${index}] must be a string`);
    }
  });
}

interface ItemFieldSpec {
  key: string;
  required: boolean;
  kind: "string" | "string-array";
}

function checkItemList(
  doc: Document.Parsed,
  text: string,
  key: string,
  fields: ItemFieldSpec[],
  issues: YamlIssue[],
  knownIds?: Set<string>,
  collectIds?: Set<string>,
): void {
  const node = resolveNode(doc, [key]);
  if (node == null) return;
  if (!isSeq(node)) {
    pushIssue(issues, text, node, `'${key}' must be a list`);
    return;
  }
  node.items.forEach((item, index) => {
    const itemNode = item as ParsedNode;
    if (!isMap(itemNode)) {
      pushIssue(issues, text, itemNode, `${key}[${index}] must be a mapping`);
      return;
    }
    for (const field of fields) {
      const fieldNode = resolveNode(doc, [key, index, field.key]);
      if (fieldNode == null) {
        if (field.required) {
          pushIssue(issues, text, itemNode, `${key}[${index}] is missing required field '${field.key}'`);
        }
        continue;
      }
      if (field.kind === "string") {
        if (!isScalar(fieldNode) || typeof fieldNode.value !== "string" || fieldNode.value === "") {
          pushIssue(issues, text, fieldNode, `${key}[${index}].${field.key} must be a non-empty string`);
        } else if (field.key === "id" && collectIds) {
          collectIds.add(String(fieldNode.value));
        }
      } else if (field.kind === "string-array") {
        if (!isSeq(fieldNode)) {
          pushIssue(issues, text, fieldNode, `${key}[${index}].${field.key} must be a list of strings`);
        } else if (knownIds) {
          fieldNode.items.forEach((refItem) => {
            const refNode = refItem as ParsedNode;
            if (isScalar(refNode) && typeof refNode.value === "string" && !knownIds.has(refNode.value)) {
              pushIssue(
                issues,
                text,
                refNode,
                `${key}[${index}].${field.key} references unknown requirement '${refNode.value}'`,
                "warning",
              );
            }
          });
        }
      }
    }
  });
}

/**
 * Validate `manifest.yaml` text against the `SpecManifest` shape.
 *
 * Returns parse errors first (line-anchored, from the YAML parser itself),
 * then structural/shape issues once the document parses. Empty on a fully
 * valid manifest.
 */
export function validateManifestYaml(text: string): YamlIssue[] {
  const issues: YamlIssue[] = [];
  if (text.trim() === "") {
    return [{ line: 1, message: "Manifest is empty", severity: "error" }];
  }

  const doc = parseDocument(text);

  for (const err of doc.errors) {
    const pos = err.linePos?.[0];
    issues.push({
      line: pos?.line ?? 1,
      column: pos?.col,
      message: err.message,
      severity: "error",
    });
  }
  for (const warn of doc.warnings) {
    const pos = warn.linePos?.[0];
    issues.push({
      line: pos?.line ?? 1,
      column: pos?.col,
      message: warn.message,
      severity: "warning",
    });
  }
  if (doc.errors.length > 0) {
    return issues;
  }

  const root = doc.contents;
  if (root == null || !isMap(root)) {
    issues.push({ line: 1, message: "Manifest must be a YAML mapping (object)", severity: "error" });
    return issues;
  }

  const idNode = resolveNode(doc, ["id"]);
  if (idNode == null || !isScalar(idNode) || typeof idNode.value !== "string" || idNode.value === "") {
    pushIssue(issues, text, idNode ?? root, "'id' is required and must be a non-empty string");
  }

  const nameNode = resolveNode(doc, ["name"]);
  if (nameNode == null || !isScalar(nameNode) || typeof nameNode.value !== "string" || nameNode.value === "") {
    pushIssue(issues, text, nameNode ?? root, "'name' is required and must be a non-empty string");
  }

  const statusNode = resolveNode(doc, ["status"]);
  if (statusNode != null) {
    const value = isScalar(statusNode) ? statusNode.value : undefined;
    if (typeof value !== "string" || !SPEC_STATUSES.includes(value as SpecStatus)) {
      pushIssue(
        issues,
        text,
        statusNode,
        `'status' must be one of: ${SPEC_STATUSES.join(", ")}`,
      );
    }
  }

  const executionModeNode = resolveNode(doc, ["execution_mode"]);
  if (executionModeNode != null) {
    const value = isScalar(executionModeNode) ? executionModeNode.value : undefined;
    if (typeof value !== "string" || !EXECUTION_MODES.includes(value as (typeof EXECUTION_MODES)[number])) {
      pushIssue(
        issues,
        text,
        executionModeNode,
        `'execution_mode' must be one of: ${EXECUTION_MODES.join(", ")}`,
      );
    }
  }

  for (const field of NULLABLE_STRING_FIELDS) {
    const node = resolveNode(doc, [field]);
    if (node != null && (!isScalar(node) || (node.value !== null && typeof node.value !== "string"))) {
      pushIssue(issues, text, node, `'${field}' must be a string or null`);
    }
  }

  for (const field of STRING_ARRAY_FIELDS) {
    checkStringList(doc, text, field, issues);
  }

  const requirementIds = new Set<string>();
  checkItemList(
    doc,
    text,
    "requirements",
    [
      { key: "id", required: true, kind: "string" },
      { key: "text", required: true, kind: "string" },
    ],
    issues,
    undefined,
    requirementIds,
  );

  checkItemList(
    doc,
    text,
    "acceptance_criteria",
    [
      { key: "id", required: true, kind: "string" },
      { key: "text", required: true, kind: "string" },
      { key: "req_refs", required: false, kind: "string-array" },
    ],
    issues,
    requirementIds,
  );

  checkItemList(
    doc,
    text,
    "open_questions",
    [
      { key: "id", required: true, kind: "string" },
      { key: "text", required: true, kind: "string" },
    ],
    issues,
  );

  checkItemList(
    doc,
    text,
    "decisions",
    [
      { key: "id", required: true, kind: "string" },
      { key: "title", required: true, kind: "string" },
    ],
    issues,
  );

  return issues;
}

export function hasErrors(issues: YamlIssue[]): boolean {
  return issues.some((issue) => issue.severity === "error");
}
