import { describe, expect, it } from "vitest";

import type { DeploymentRead, EnvironmentRead } from "@/lib/api/types";

import {
  actorLabel,
  canCancel,
  canDecide,
  canRollback,
  checkNameMeta,
  checkStatusMeta,
  formatRelativeTime,
  healthMeta,
  isTerminalState,
  nextEnvironmentName,
  pickDefaultDeploymentId,
  shortSha,
  sortDeploymentsForQueue,
  sortEnvironmentsByRank,
  stateMeta,
  toneBadgeClass,
  toneDotClass,
} from "./deployment-meta";

function env(name: string, rank: number, live?: Partial<DeploymentRead>): EnvironmentRead {
  return {
    id: `env-${name}`,
    name,
    rank,
    is_restricted: false,
    requires_approval: true,
    gate_config: {},
    provider_config: {},
    health_check: {},
    currently_deployed: live
      ? ({ id: `d-${name}`, commit_sha: "abc1234", state: "succeeded", ...live } as DeploymentRead)
      : null,
  };
}

function dep(id: string, over: Partial<DeploymentRead> = {}): DeploymentRead {
  return {
    id,
    project_id: "p1",
    environment_name: "staging",
    repo_id: "acme/web",
    commit_sha: "9f3c1a2b7d",
    kind: "promotion",
    state: "succeeded",
    trigger: "manual",
    initiated_by: "user:alice",
    requested_at: "2026-07-05T10:00:00Z",
    ...over,
  };
}

describe("stateMeta", () => {
  it("labels and tones the FSM states", () => {
    expect(stateMeta("awaiting_approval").label).toBe("Awaiting approval");
    expect(stateMeta("awaiting_approval").tone).toBe("warning");
    expect(stateMeta("succeeded").tone).toBe("success");
    expect(stateMeta("failed").tone).toBe("danger");
    expect(stateMeta("gate_rejected").tone).toBe("danger");
  });

  it("marks mid-flight states active (for the spinner)", () => {
    expect(stateMeta("deploying").active).toBe(true);
    expect(stateMeta("verifying").active).toBe(true);
    expect(stateMeta("succeeded").active).toBe(false);
  });
});

describe("terminal + action guards", () => {
  it("identifies terminal states", () => {
    expect(isTerminalState("succeeded")).toBe(true);
    expect(isTerminalState("cancelled")).toBe(true);
    expect(isTerminalState("awaiting_approval")).toBe(false);
  });

  it("allows decisions only when awaiting approval", () => {
    expect(canDecide("awaiting_approval")).toBe(true);
    expect(canDecide("deploying")).toBe(false);
  });

  it("allows cancel on non-terminal states only", () => {
    expect(canCancel("deploying")).toBe(true);
    expect(canCancel("awaiting_approval")).toBe(true);
    expect(canCancel("succeeded")).toBe(false);
  });

  it("allows rollback only on succeeded", () => {
    expect(canRollback("succeeded")).toBe(true);
    expect(canRollback("failed")).toBe(false);
  });
});

describe("health + check metadata", () => {
  it("maps health status", () => {
    expect(healthMeta("passing").tone).toBe("success");
    expect(healthMeta("failing").tone).toBe("danger");
    expect(healthMeta(null).label).toBe("Unknown");
  });

  it("labels gate check names + statuses", () => {
    expect(checkNameMeta("ci_green").label).toBe("CI green");
    expect(checkNameMeta("not_frozen").label).toBe("Not frozen");
    expect(checkStatusMeta("passed").tone).toBe("success");
    expect(checkStatusMeta("failed").tone).toBe("danger");
    expect(checkStatusMeta("pending").tone).toBe("warning");
    expect(checkStatusMeta("skipped").tone).toBe("muted");
  });
});

describe("pipeline env helpers", () => {
  const envs = [env("prod", 2), env("dev", 0), env("staging", 1)];

  it("sorts environments by rank (dev -> staging -> prod)", () => {
    expect(sortEnvironmentsByRank(envs).map((e) => e.name)).toEqual([
      "dev",
      "staging",
      "prod",
    ]);
  });

  it("finds the next promotion target by rank", () => {
    expect(nextEnvironmentName(envs, "dev")).toBe("staging");
    expect(nextEnvironmentName(envs, "staging")).toBe("prod");
    expect(nextEnvironmentName(envs, "prod")).toBeNull();
    expect(nextEnvironmentName(envs, undefined)).toBe("dev");
    expect(nextEnvironmentName([], "dev")).toBeNull();
  });
});

describe("deployment list ordering", () => {
  it("ranks awaiting-approval first, then most recent", () => {
    const list = [
      dep("a", { state: "succeeded", requested_at: "2026-07-05T12:00:00Z" }),
      dep("b", { state: "awaiting_approval", requested_at: "2026-07-05T09:00:00Z" }),
      dep("c", { state: "awaiting_approval", requested_at: "2026-07-05T11:00:00Z" }),
    ];
    const ordered = sortDeploymentsForQueue(list).map((d) => d.id);
    expect(ordered[0]).toBe("c"); // awaiting + newest
    expect(ordered[1]).toBe("b"); // awaiting + older
    expect(ordered[2]).toBe("a");
    expect(pickDefaultDeploymentId(list)).toBe("c");
  });

  it("returns null for an empty list", () => {
    expect(pickDefaultDeploymentId([])).toBeNull();
  });
});

describe("formatting helpers", () => {
  it("shortens a commit sha to 7 chars", () => {
    expect(shortSha("9f3c1a2b7d8e")).toBe("9f3c1a2");
    expect(shortSha("abc")).toBe("abc");
    expect(shortSha(null)).toBe("—");
  });

  it("strips actor prefixes", () => {
    expect(actorLabel("user:alice")).toBe("alice");
    expect(actorLabel("agent:bot-7")).toBe("bot-7");
    expect(actorLabel("system")).toBe("system");
    expect(actorLabel(null)).toBe("—");
  });

  it("formats relative time against a fixed now", () => {
    const now = Date.parse("2026-07-05T12:00:00Z");
    expect(formatRelativeTime("2026-07-05T11:59:50Z", now)).toBe("just now");
    expect(formatRelativeTime("2026-07-05T11:55:00Z", now)).toBe("5m ago");
    expect(formatRelativeTime("2026-07-05T09:00:00Z", now)).toBe("3h ago");
    expect(formatRelativeTime("2026-07-03T12:00:00Z", now)).toBe("2d ago");
    expect(formatRelativeTime(null, now)).toBe("—");
  });
});

describe("tone classes", () => {
  it("returns token-based classes (never hardcoded colours)", () => {
    expect(toneBadgeClass("success")).toContain("text-success");
    expect(toneBadgeClass("danger")).toContain("bg-danger/10");
    expect(toneBadgeClass("muted")).toContain("text-muted-foreground");
    expect(toneDotClass("warning")).toBe("bg-warning");
    expect(toneDotClass("info")).toBe("bg-primary");
  });
});
