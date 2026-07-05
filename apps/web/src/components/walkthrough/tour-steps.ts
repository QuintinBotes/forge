/**
 * The guided-walkthrough script.
 *
 * Framework-free step definitions (no JSX) so ordering, targets and the
 * loop-stage mapping stay unit testable. The walkthrough highlights real,
 * navigable UI — each stop is a stage of the Forge build loop and deep-links
 * into the product route that runs it.
 */

import type { OnboardingStepKey } from "@/lib/api/types";

export interface TourStep {
  /** Stable id (also the analytics/step key). */
  id: string;
  /** `data-tour` anchor this step spotlights on the page. */
  target: string;
  /** Coach-mark heading. */
  title: string;
  /** Coach-mark body copy. */
  body: string;
  /** The loop stage this step teaches (drives the live progress tie-in). */
  progressKey?: OnboardingStepKey;
  /** Deep link the step's call-to-action opens in the product. */
  href?: string;
  /** Label for that call-to-action. */
  cta?: string;
}

/** The tour, in order: an orientation stop then the four loop stages. */
export const TOUR_STEPS: readonly TourStep[] = [
  {
    id: "welcome",
    target: "loop",
    title: "The Forge loop",
    body: "Every change flows through one loop: draft a spec, let an agent build it, review the pull request, then merge. This tour walks you through each stop — you can leave any time and pick up where you left off.",
  },
  {
    id: "spec",
    target: "stop-spec",
    title: "1 · Create a spec",
    body: "Start by capturing intent as a spec: requirements, acceptance criteria and open questions. The spec is the contract an agent builds against and the human gate that approves the work.",
    progressKey: "spec",
    href: "/specs",
    cta: "Open Specs",
  },
  {
    id: "run",
    target: "stop-run",
    title: "2 · Run an agent",
    body: "Once a spec is approved, dispatch an agent. It plans, writes code and opens a pull request — every step recorded as a replayable run trace you can inspect.",
    progressKey: "run",
    href: "/runs",
    cta: "Open Runs",
  },
  {
    id: "review",
    target: "stop-review",
    title: "3 · Review the PR",
    body: "The agent's pull request lands as an approval gate. Review the diff, checks and traceability, then approve, request changes, or escalate — the human stays in the loop.",
    progressKey: "review",
    href: "/approvals",
    cta: "Open Approvals",
  },
  {
    id: "merge",
    target: "stop-merge",
    title: "4 · Merge & ship",
    body: "Approve the gate to merge and promote the change through your deployment pipeline. That closes the loop — and you're ready to draft the next spec.",
    progressKey: "merge",
    href: "/deployments",
    cta: "Open Deployments",
  },
];

export const TOUR_STEP_COUNT = TOUR_STEPS.length;

/** Clamp an index into the valid step range. */
export function clampStepIndex(index: number): number {
  if (Number.isNaN(index) || index < 0) return 0;
  if (index > TOUR_STEPS.length - 1) return TOUR_STEPS.length - 1;
  return Math.floor(index);
}

/** True when `index` is the final step (its primary action finishes the tour). */
export function isLastStep(index: number): boolean {
  return index >= TOUR_STEPS.length - 1;
}
