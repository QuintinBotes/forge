/**
 * Pure helpers backing Guided mode — auto-numbering, Given/When/Then
 * composition, and the soft validation nudges + Ready-to-create checklist /
 * coverage meter. Kept dependency-free (no React) so they're trivially unit
 * tested and reusable from both the form and its summary panel.
 */

import type { AcceptanceCriterion, ADR, Requirement, SpecManifest } from "@/lib/api/types";

/**
 * The next sequential id for a prefix (`"R"` / `"AC"` / `"ADR"`) given the ids
 * already in use — scans for `${prefix}<number>` and returns one past the
 * highest match (or `${prefix}1` when none match), so ids stay auto-numbered
 * even after items in the middle are removed.
 */
export function nextSequentialId(prefix: string, existingIds: readonly string[]): string {
  const pattern = new RegExp(`^${prefix}(\\d+)$`);
  let max = 0;
  for (const id of existingIds) {
    const match = pattern.exec(id);
    if (match) {
      const n = Number.parseInt(match[1], 10);
      if (n > max) max = n;
    }
  }
  return `${prefix}${max + 1}`;
}

export interface GivenWhenThen {
  given: string;
  when: string;
  then: string;
}

/**
 * Best-effort split of an AC's free-text `text` into Given/When/Then parts.
 * Each keyword is matched independently (not as one all-or-nothing pattern),
 * so a still-partial edit — e.g. only "Given ..." typed so far — round-trips
 * without losing what's already there.
 */
export function parseGivenWhenThen(text: string): GivenWhenThen {
  const trimmed = text.trim();
  const givenMatch = /Given\s+(.*?)(?=\s+When\s+|\s+Then\s+|$)/is.exec(trimmed);
  const whenMatch = /When\s+(.*?)(?=\s+Then\s+|$)/is.exec(trimmed);
  const thenMatch = /Then\s+(.*)$/is.exec(trimmed);
  if (!givenMatch && !whenMatch && !thenMatch) {
    return { given: "", when: "", then: trimmed };
  }
  return {
    given: givenMatch ? givenMatch[1].trim() : "",
    when: whenMatch ? whenMatch[1].trim() : "",
    then: thenMatch ? thenMatch[1].trim() : "",
  };
}

/** Compose Given/When/Then parts back into the AC's single `text` field. */
export function composeGivenWhenThen({ given, when, then }: GivenWhenThen): string {
  const parts: string[] = [];
  if (given) parts.push(`Given ${given}`);
  if (when) parts.push(`When ${when}`);
  if (then) parts.push(`Then ${then}`);
  return parts.join(" ");
}

/**
 * The three first-class acceptance-criterion authoring styles. Every style is
 * encoded losslessly inside the criterion's single `text` field, so switching
 * style never touches its `req_refs` (R#) links. Mirrors
 * `forge_spec.criteria.classify_criterion` on the backend.
 */
export type CriterionStyle = "gherkin" | "assertion" | "checklist";

/** `- [ ] label` / `- [x] label` — the checked box is case-insensitive. */
const CHECK_ITEM = /^- \[([ xX])\] ?(.*)$/;
const GHERKIN_KEYWORD = /\b(?:given|when|then)\b/i;

/** One checklist entry: a `label` and whether its box is `checked`. */
export interface CheckItem {
  label: string;
  checked: boolean;
}

function nonBlankLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

/**
 * Infer a criterion's authoring style from its `text` (never throws). Blank
 * text defaults to `"gherkin"` (the editor's default shape); a `text` whose
 * every non-blank line is a check item is a `"checklist"` even if a label
 * contains a Gherkin keyword; otherwise Gherkin keywords => `"gherkin"`, and
 * anything else is a plain `"assertion"`.
 */
export function classifyCriterionStyle(text: string): CriterionStyle {
  const lines = nonBlankLines(text);
  if (lines.length === 0) return "gherkin";
  if (lines.every((line) => CHECK_ITEM.test(line))) return "checklist";
  if (GHERKIN_KEYWORD.test(text)) return "gherkin";
  return "assertion";
}

/** Parse checklist `text` into items (a non-item line becomes an unchecked item). */
export function parseChecklist(text: string): CheckItem[] {
  return nonBlankLines(text).map((line) => {
    const match = CHECK_ITEM.exec(line);
    if (!match) return { label: line, checked: false };
    return { label: match[2].trim(), checked: match[1] === "x" || match[1] === "X" };
  });
}

/** Render checklist `items` back to canonical `- [ ] label` lines. */
export function composeChecklist(items: readonly CheckItem[]): string {
  return items.map((item) => `- [${item.checked ? "x" : " "}] ${item.label}`.trimEnd()).join("\n");
}

/**
 * Re-encode a criterion's `text` for a new `target` style, preserving prose
 * where it makes sense so switching styles doesn't silently lose content.
 */
export function convertCriterionText(text: string, target: CriterionStyle): string {
  const current = classifyCriterionStyle(text);
  if (current === target) return text;
  if (target === "checklist") {
    const label = text.trim();
    return composeChecklist([{ label, checked: false }]);
  }
  if (current === "checklist") {
    const labels = parseChecklist(text)
      .map((item) => item.label)
      .filter(Boolean);
    // Gherkin editor will re-parse the joined prose; assertion keeps it flat.
    return labels.join(target === "gherkin" ? " " : "; ");
  }
  // gherkin <-> assertion share the same flat prose encoding.
  return text;
}

/** A single soft-validation nudge — never blocking, just surfaced guidance. */
export interface Nudge {
  id: string;
  message: string;
}

/** Non-blocking nudges: gaps a human should notice before creating the spec. */
export function computeNudges(manifest: SpecManifest): Nudge[] {
  const nudges: Nudge[] = [];
  const requirements = manifest.requirements ?? [];
  const criteria = manifest.acceptance_criteria ?? [];

  if (!manifest.name.trim()) {
    nudges.push({ id: "no-goal", message: "The goal is empty — describe what this spec achieves." });
  }
  if (requirements.length === 0) {
    nudges.push({ id: "no-requirements", message: "Add at least one requirement." });
  }
  if (requirements.length > 0 && criteria.length === 0) {
    nudges.push({
      id: "no-criteria",
      message: "Add acceptance criteria so requirements can be verified.",
    });
  }
  for (const req of requirements) {
    const linked = criteria.some((ac) => (ac.req_refs ?? []).includes(req.id));
    if (!linked) {
      nudges.push({
        id: `uncovered-${req.id}`,
        message: `${req.id} has no linked acceptance criterion.`,
      });
    }
  }
  for (const req of requirements) {
    if (!req.text.trim()) {
      nudges.push({ id: `empty-req-${req.id}`, message: `${req.id} has no description yet.` });
    }
  }
  for (const ac of criteria) {
    if ((ac.req_refs ?? []).length === 0) {
      nudges.push({ id: `unlinked-${ac.id}`, message: `${ac.id} isn't linked to a requirement.` });
    }
  }
  const openQuestions = manifest.open_questions ?? [];
  const unresolved = openQuestions.filter((q) => !q.resolution);
  if (unresolved.length > 0) {
    nudges.push({
      id: "open-questions",
      message: `${unresolved.length} open question${unresolved.length === 1 ? "" : "s"} still unresolved.`,
    });
  }
  return nudges;
}

/** Requirement coverage: the fraction of requirements with >=1 linked AC. */
export interface CoverageSummary {
  satisfied: number;
  total: number;
  pct: number;
}

export function computeCoverage(manifest: SpecManifest): CoverageSummary {
  const requirements = manifest.requirements ?? [];
  const criteria = manifest.acceptance_criteria ?? [];
  const total = requirements.length;
  const satisfied = requirements.filter((req) =>
    criteria.some((ac) => (ac.req_refs ?? []).includes(req.id)),
  ).length;
  const pct = total > 0 ? Math.round((satisfied / total) * 100) : 0;
  return { satisfied, total, pct };
}

/** One line of the Ready-to-create checklist. */
export interface ChecklistItem {
  id: string;
  label: string;
  done: boolean;
}

/** The Ready-to-create checklist — the minimum bar for a spec worth reviewing. */
export function computeChecklist(manifest: SpecManifest): ChecklistItem[] {
  const requirements = manifest.requirements ?? [];
  const criteria = manifest.acceptance_criteria ?? [];
  const coverage = computeCoverage(manifest);
  return [
    { id: "goal", label: "Goal is filled in", done: manifest.name.trim().length > 0 },
    { id: "requirements", label: "At least one requirement", done: requirements.length > 0 },
    {
      id: "criteria",
      label: "At least one acceptance criterion",
      done: criteria.length > 0,
    },
    {
      id: "coverage",
      label: "Every requirement has a linked acceptance criterion",
      done: requirements.length > 0 && coverage.satisfied === coverage.total,
    },
  ];
}

export function addRequirement(requirements: Requirement[]): Requirement[] {
  const id = nextSequentialId("R", requirements.map((r) => r.id));
  return [...requirements, { id, text: "" }];
}

export function addAcceptanceCriterion(criteria: AcceptanceCriterion[]): AcceptanceCriterion[] {
  const id = nextSequentialId("AC", criteria.map((c) => c.id));
  return [...criteria, { id, text: "", req_refs: [] }];
}

export function addAdr(decisions: ADR[]): ADR[] {
  const id = nextSequentialId("ADR", decisions.map((d) => d.id));
  return [...decisions, { id, title: "", status: "proposed" }];
}
