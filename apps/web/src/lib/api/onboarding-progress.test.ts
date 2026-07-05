import { describe, expect, it } from "vitest";

import { deriveOnboardingProgress } from "./onboarding-progress";
import type {
  ApprovalSummary,
  DeploymentRead,
  DeploymentState,
  GateType,
  SpecOverview,
  SpecStatus,
} from "./types";

function spec(status: SpecStatus): SpecOverview {
  return { id: `spec-${status}`, name: `Spec ${status}`, status };
}

function approval(gate_type: GateType): ApprovalSummary {
  return {
    id: `appr-${gate_type}-${Math.random()}`,
    gate_type,
    status: "pending",
    title: `${gate_type} gate`,
  };
}

function deployment(state: DeploymentState): DeploymentRead {
  return {
    id: `dep-${state}-${Math.random()}`,
    project_id: "default",
    environment_name: "production",
    repo_id: "repo",
    commit_sha: "abc123",
    kind: "promotion",
    state,
    trigger: "manual",
    initiated_by: "user",
    requested_at: "2026-01-01T00:00:00Z",
  };
}

describe("deriveOnboardingProgress", () => {
  it("reports every stage incomplete for an empty workspace", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [],
      approvals: [],
      deployments: [],
    });
    expect(p.projectId).toBe("default");
    expect(p.totalCount).toBe(4);
    expect(p.completedCount).toBe(0);
    expect(p.allComplete).toBe(false);
    expect(p.steps.map((s) => s.done)).toEqual([false, false, false, false]);
    expect(p.steps.map((s) => s.key)).toEqual(["spec", "run", "review", "merge"]);
  });

  it("marks the spec stage done once any spec exists (draft counts)", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [spec("draft"), spec("clarifying")],
      approvals: [],
      deployments: [],
    });
    const stepSpec = p.steps.find((s) => s.key === "spec")!;
    const stepRun = p.steps.find((s) => s.key === "run")!;
    expect(stepSpec).toMatchObject({ done: true, count: 2 });
    // A draft spec has not reached `implementing`, so no agent has run yet.
    expect(stepRun).toMatchObject({ done: false, count: 0 });
  });

  it("marks the run stage done once a spec reaches implementing or beyond", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [spec("draft"), spec("implementing"), spec("validated")],
      approvals: [],
      deployments: [],
    });
    expect(p.steps.find((s) => s.key === "run")).toMatchObject({
      done: true,
      count: 2,
    });
  });

  it("counts only PR-type approvals toward the review stage", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [],
      approvals: [approval("spec"), approval("pr"), approval("deploy")],
      deployments: [],
    });
    expect(p.steps.find((s) => s.key === "review")).toMatchObject({
      done: true,
      count: 1,
    });
  });

  it("counts only succeeded deployments toward the merge stage", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [],
      approvals: [],
      deployments: [
        deployment("deploying"),
        deployment("succeeded"),
        deployment("failed"),
      ],
    });
    expect(p.steps.find((s) => s.key === "merge")).toMatchObject({
      done: true,
      count: 1,
    });
  });

  it("reports allComplete when every stage has a real artifact", () => {
    const p = deriveOnboardingProgress("default", {
      specs: [spec("validated")],
      approvals: [approval("pr")],
      deployments: [deployment("succeeded")],
    });
    expect(p.completedCount).toBe(4);
    expect(p.allComplete).toBe(true);
  });
});
