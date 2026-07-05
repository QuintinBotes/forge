import { describe, expect, it } from "vitest";

import {
  actorLabel,
  blastMeta,
  eventMeta,
  humanize,
  isResolvedState,
  lifecycleMeta,
  relativeTime,
  severityMeta,
  stepStatusClass,
} from "./incident-meta";

describe("severityMeta", () => {
  it("ranks critical above low", () => {
    expect(severityMeta("critical").weight).toBeGreaterThan(
      severityMeta("low").weight,
    );
  });

  it("gives critical a solid danger badge", () => {
    expect(severityMeta("critical").badgeClass).toContain("bg-danger");
    expect(severityMeta("critical").label).toBe("Critical");
  });

  it("falls back to low for an unknown severity", () => {
    expect(severityMeta("nonsense").label).toBe("Low");
  });
});

describe("lifecycleMeta", () => {
  it("maps a known state to a label + tone class", () => {
    const meta = lifecycleMeta("awaiting_approval");
    expect(meta.label).toBe("Awaiting approval");
    expect(meta.badgeClass).toContain("text-primary");
    expect(meta.icon).toBeTypeOf("object");
  });

  it("resolves the resolved state to a success tone", () => {
    expect(lifecycleMeta("resolved").badgeClass).toContain("text-success");
  });

  it("humanizes an unknown lifecycle state", () => {
    expect(lifecycleMeta("some_new_state").label).toBe("Some new state");
  });
});

describe("isResolvedState", () => {
  it("is true for resolved/postmortem/closed", () => {
    expect(isResolvedState("resolved")).toBe(true);
    expect(isResolvedState("postmortem_created")).toBe(true);
    expect(isResolvedState("closed")).toBe(true);
  });
  it("is false for active states", () => {
    expect(isResolvedState("monitoring")).toBe(false);
  });
});

describe("blastMeta", () => {
  it("colors high blast radius as danger", () => {
    expect(blastMeta("high").badgeClass).toContain("text-danger");
  });
  it("colors low blast radius as success", () => {
    expect(blastMeta("low").badgeClass).toContain("text-success");
  });
  it("labels a missing radius as unknown", () => {
    expect(blastMeta(null).label).toMatch(/unknown/i);
  });
});

describe("stepStatusClass", () => {
  it("uses success for succeeded and danger for failed", () => {
    expect(stepStatusClass("succeeded")).toContain("text-success");
    expect(stepStatusClass("failed")).toContain("text-danger");
  });
});

describe("eventMeta", () => {
  it("labels acknowledge as an advance action", () => {
    const meta = eventMeta("incident_acknowledged");
    expect(meta.label).toBe("Acknowledge");
    expect(meta.intent).toBe("advance");
  });

  it("marks rejection and cancellation as danger", () => {
    expect(eventMeta("remediation_rejected").intent).toBe("danger");
    expect(eventMeta("cancel").intent).toBe("danger");
  });

  it("marks approvals as approve", () => {
    expect(eventMeta("remediation_approved").intent).toBe("approve");
  });

  it("humanizes an unknown event token", () => {
    expect(eventMeta("brand_new_event").label).toBe("Brand new event");
  });
});

describe("relativeTime", () => {
  it("returns a dash for empty input", () => {
    expect(relativeTime(null)).toBe("—");
  });
  it("formats hours ago", () => {
    const now = Date.parse("2026-07-05T12:00:00Z");
    expect(relativeTime("2026-07-05T09:00:00Z", now)).toBe("3h ago");
  });
  it("returns just now for very recent times", () => {
    const now = Date.parse("2026-07-05T12:00:00Z");
    expect(relativeTime("2026-07-05T11:59:50Z", now)).toBe("just now");
  });
});

describe("actorLabel", () => {
  it("labels the system actor", () => {
    expect(actorLabel("system")).toBe("System");
    expect(actorLabel(null)).toBe("System");
  });
  it("shortens a kind:uuid actor", () => {
    expect(actorLabel("user:abcdef1234567890")).toBe("User abcdef12");
  });
});

describe("humanize", () => {
  it("title-cases a snake token", () => {
    expect(humanize("runbook_completed")).toBe("Runbook completed");
  });
});
