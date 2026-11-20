import { describe, expect, it } from "vitest";

import {
  ACTION_META,
  actionForKey,
  gateMeta,
  riskBadgeClass,
  statusBadgeClass,
} from "./approval-meta";

describe("actionForKey (spec a/r/x/e map)", () => {
  it("maps each shortcut to its action", () => {
    expect(actionForKey("a")).toBe("approve");
    expect(actionForKey("x")).toBe("reject");
    expect(actionForKey("r")).toBe("request_changes");
    expect(actionForKey("e")).toBe("escalate");
  });

  it("is case-insensitive and null for unmapped keys", () => {
    expect(actionForKey("A")).toBe("approve");
    expect(actionForKey("z")).toBeNull();
  });

  it("only reject and request_changes require a reason", () => {
    expect(ACTION_META.approve.requiresNote).toBe(false);
    expect(ACTION_META.escalate.requiresNote).toBe(false);
    expect(ACTION_META.reject.requiresNote).toBe(true);
    expect(ACTION_META.request_changes.requiresNote).toBe(true);
  });
});

describe("styling helpers use tokens only", () => {
  it("returns danger tokens for critical risk and rejected status", () => {
    expect(riskBadgeClass("critical")).toContain("text-danger");
    expect(statusBadgeClass("rejected")).toContain("text-danger");
    expect(statusBadgeClass("approved")).toContain("text-success");
  });

  it("falls back gracefully for unknown gates", () => {
    // @ts-expect-error — exercising the runtime fallback path.
    expect(gateMeta("mystery").label).toBe("mystery");
  });
});
