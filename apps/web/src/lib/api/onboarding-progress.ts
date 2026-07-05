/**
 * Pure derivation for the guided-walkthrough onboarding progress.
 *
 * Framework-free (no React, no client) so the loop-stage roll-up is unit
 * testable in isolation and can be reused by both the typed API client
 * ({@link ForgeApiClient.getOnboardingProgress}) and the query hook.
 *
 * Each stage of the "create a spec -> run an agent -> review the PR -> merge"
 * loop is grounded in a real router read:
 *   - spec   : GET /projects/{id}/specs   (a spec exists)
 *   - run    : same specs, in `implementing` or beyond (an agent has begun work)
 *   - review : GET /approvals             (a PR review gate exists)
 *   - merge  : GET /projects/{id}/deployments (a deployment has succeeded)
 */

import {
  ONBOARDING_STEP_KEYS,
  SPEC_STATUSES,
  type ApprovalSummary,
  type DeploymentRead,
  type OnboardingProgress,
  type OnboardingStepKey,
  type OnboardingStepProgress,
  type SpecOverview,
  type SpecStatus,
} from "./types";

/** Lifecycle index at which an agent is considered to have started running. */
const IMPLEMENTING_INDEX = SPEC_STATUSES.indexOf("implementing");

/** True once a spec has advanced to `implementing` (or beyond). */
function specIsUnderway(spec: SpecOverview): boolean {
  const status = spec.status as SpecStatus | undefined;
  const index = status ? SPEC_STATUSES.indexOf(status) : -1;
  return index >= IMPLEMENTING_INDEX;
}

export interface OnboardingInputs {
  specs: SpecOverview[];
  approvals: ApprovalSummary[];
  deployments: DeploymentRead[];
}

/**
 * Roll the three live reads up into the ordered four-stage loop progress.
 * Every stage is independent (a later stage can be "done" without the earlier
 * one) so the walkthrough surfaces the true state rather than a strict gate.
 */
export function deriveOnboardingProgress(
  projectId: string,
  { specs, approvals, deployments }: OnboardingInputs,
): OnboardingProgress {
  const specCount = specs.length;
  const runCount = specs.filter(specIsUnderway).length;
  const reviewCount = approvals.filter((a) => a.gate_type === "pr").length;
  const mergeCount = deployments.filter((d) => d.state === "succeeded").length;

  const countFor: Record<OnboardingStepKey, number> = {
    spec: specCount,
    run: runCount,
    review: reviewCount,
    merge: mergeCount,
  };

  const steps: OnboardingStepProgress[] = ONBOARDING_STEP_KEYS.map((key) => ({
    key,
    count: countFor[key],
    done: countFor[key] > 0,
  }));

  const completedCount = steps.filter((s) => s.done).length;

  return {
    projectId,
    steps,
    completedCount,
    totalCount: steps.length,
    allComplete: completedCount === steps.length,
  };
}
