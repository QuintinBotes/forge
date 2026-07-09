/**
 * Starter templates for `/specs/new` (ss-entry). Each seeds a skeleton
 * requirement + acceptance criterion (and, for bugfix/spike, a constraint)
 * onto a fresh draft manifest — a starting shape for the Guided-mode form,
 * not a locked-in answer. Applying a template never clobbers anything the
 * author has already typed into requirements / acceptance criteria /
 * constraints; it only fills in what's still empty.
 */

import type { AcceptanceCriterion, Requirement, SpecManifest } from "@/lib/api/types";

export type SpecTemplateId = "feature" | "bugfix" | "spike";

export interface SpecTemplateSeed {
  id: SpecTemplateId;
  label: string;
  description: string;
  requirements: Requirement[];
  acceptanceCriteria: AcceptanceCriterion[];
  constraints: string[];
}

export const SPEC_TEMPLATES: readonly SpecTemplateSeed[] = [
  {
    id: "feature",
    label: "Feature",
    description: "A new capability end-to-end, from requirement to acceptance criteria.",
    requirements: [{ id: "R1", text: "Describe the new capability the user gains." }],
    acceptanceCriteria: [
      {
        id: "AC1",
        text: "Given <context>, when <action>, then <outcome>.",
        req_refs: ["R1"],
      },
    ],
    constraints: [],
  },
  {
    id: "bugfix",
    label: "Bugfix",
    description: "Pin down a regression with a reproducing case and the expected behavior.",
    requirements: [
      { id: "R1", text: "Describe the incorrect behavior and the behavior expected instead." },
    ],
    acceptanceCriteria: [
      {
        id: "AC1",
        text:
          "Given the steps that reproduce the bug, when they're applied, then the expected behavior occurs (a regression test is added).",
        req_refs: ["R1"],
      },
    ],
    constraints: ["Scoped to the regression — no unrelated behavior changes."],
  },
  {
    id: "spike",
    label: "Spike",
    description: "A time-boxed investigation that answers an open question before committing.",
    requirements: [{ id: "R1", text: "State the question this spike must answer." }],
    acceptanceCriteria: [
      {
        id: "AC1",
        text:
          "Given the investigation is complete, when findings are written up, then a recommended approach and its tradeoffs are documented.",
        req_refs: ["R1"],
      },
    ],
    constraints: ["Time-boxed — produces a decision/recommendation, not production code."],
  },
];

export function specTemplate(id: SpecTemplateId): SpecTemplateSeed {
  const found = SPEC_TEMPLATES.find((t) => t.id === id);
  if (!found) {
    throw new Error(`Unknown spec template: ${id}`);
  }
  return found;
}

/**
 * Seed `draft` with `templateId`'s starter requirement/acceptance
 * criterion/constraints. Fields the author already populated are left
 * untouched — a template only fills in what's still empty, so switching
 * templates (or picking one after typing) never destroys drafted work.
 */
export function applySpecTemplate(
  templateId: SpecTemplateId,
  draft: SpecManifest,
): SpecManifest {
  const template = specTemplate(templateId);
  return {
    ...draft,
    requirements: draft.requirements?.length
      ? draft.requirements
      : template.requirements.map((r) => ({ ...r })),
    acceptance_criteria: draft.acceptance_criteria?.length
      ? draft.acceptance_criteria
      : template.acceptanceCriteria.map((a) => ({ ...a })),
    constraints: draft.constraints?.length ? draft.constraints : [...template.constraints],
  };
}
