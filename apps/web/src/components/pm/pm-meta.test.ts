import { describe, expect, it } from "vitest";

import {
  conflictPolicyLabel,
  connectionStatusMeta,
  isConnectionEnabled,
  providerLabel,
  relativeTime,
  rowsToStatusMap,
  statusMapToRows,
  summarizeLinks,
  syncStateMeta,
} from "./pm-meta";

const NOW = Date.parse("2026-07-05T12:00:00Z");

describe("labels", () => {
  it("names providers and policies for humans", () => {
    expect(providerLabel("jira")).toBe("Jira");
    expect(providerLabel("linear")).toBe("Linear");
    expect(providerLabel("asana")).toBe("Asana");
    expect(providerLabel("monday")).toBe("monday.com");
    expect(providerLabel("github_projects")).toBe("GitHub Projects");
    expect(providerLabel("clickup")).toBe("ClickUp");
    expect(providerLabel("trello")).toBe("Trello");
    expect(providerLabel("gitlab")).toBe("GitLab");
    expect(providerLabel("generic")).toBe("Custom (generic)");
    expect(conflictPolicyLabel("newest_wins")).toBe("Newest wins");
    expect(conflictPolicyLabel("external_wins")).toBe("Provider wins");
  });
});

describe("status tones", () => {
  it("maps connection status to a semantic tone", () => {
    expect(connectionStatusMeta("connected")).toEqual({
      label: "Connected",
      tone: "success",
    });
    expect(connectionStatusMeta("error").tone).toBe("danger");
    expect(connectionStatusMeta("disabled").tone).toBe("muted");
  });

  it("maps sync state to a tone", () => {
    expect(syncStateMeta("synced").tone).toBe("success");
    expect(syncStateMeta("conflict").tone).toBe("warning");
    expect(syncStateMeta("error").tone).toBe("danger");
    expect(syncStateMeta("pending_out").tone).toBe("info");
  });

  it("treats every non-disabled status as enabled", () => {
    expect(isConnectionEnabled("connected")).toBe(true);
    expect(isConnectionEnabled("pending")).toBe(true);
    expect(isConnectionEnabled("disabled")).toBe(false);
  });
});

describe("summarizeLinks", () => {
  it("rolls counts up and folds both pending states together", () => {
    const rollup = summarizeLinks({
      synced: 6,
      pending_out: 1,
      pending_in: 2,
      conflict: 1,
      error: 0,
    });
    expect(rollup.total).toBe(10);
    expect(rollup.pending).toBe(3);
    expect(rollup.conflicts).toBe(1);
    expect(rollup.healthyFraction).toBeCloseTo(0.6);
  });

  it("treats an empty map as trivially healthy", () => {
    const rollup = summarizeLinks(undefined);
    expect(rollup.total).toBe(0);
    expect(rollup.healthyFraction).toBe(1);
  });
});

describe("status-map ↔ rows", () => {
  it("explodes a map into alphabetically stable rows", () => {
    const rows = statusMapToRows({ "In Review": "started", Done: "completed" });
    expect(rows).toEqual([
      { external: "Done", category: "completed" },
      { external: "In Review", category: "started" },
    ]);
  });

  it("collapses rows back, dropping blanks and letting the last dup win", () => {
    const map = rowsToStatusMap([
      { external: "  ", category: "backlog" },
      { external: "Done", category: "started" },
      { external: "Done", category: "completed" },
    ]);
    expect(map).toEqual({ Done: "completed" });
  });
});

describe("relativeTime", () => {
  it("formats recent and older timestamps", () => {
    expect(relativeTime(null)).toBe("—");
    expect(relativeTime("2026-07-05T11:59:40Z", NOW)).toBe("just now");
    expect(relativeTime("2026-07-05T11:30:00Z", NOW)).toBe("30m ago");
    expect(relativeTime("2026-07-05T09:00:00Z", NOW)).toBe("3h ago");
    expect(relativeTime("2026-07-03T12:00:00Z", NOW)).toBe("2d ago");
  });
});
