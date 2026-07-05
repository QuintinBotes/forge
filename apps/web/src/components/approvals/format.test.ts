import { describe, expect, it } from "vitest";

import { actorLabel, humanizeKey, relativeTime } from "./format";

const NOW = Date.parse("2026-07-05T12:00:00Z");

describe("relativeTime", () => {
  it("returns an em dash for missing/invalid input", () => {
    expect(relativeTime(null, NOW)).toBe("—");
    expect(relativeTime(undefined, NOW)).toBe("—");
    expect(relativeTime("not-a-date", NOW)).toBe("—");
  });

  it("bins recent timestamps into compact units", () => {
    expect(relativeTime("2026-07-05T11:59:50Z", NOW)).toBe("just now");
    expect(relativeTime("2026-07-05T11:30:00Z", NOW)).toBe("30m ago");
    expect(relativeTime("2026-07-05T09:00:00Z", NOW)).toBe("3h ago");
    expect(relativeTime("2026-07-03T12:00:00Z", NOW)).toBe("2d ago");
  });

  it("treats future timestamps as just now", () => {
    expect(relativeTime("2026-07-05T12:05:00Z", NOW)).toBe("just now");
  });
});

describe("actorLabel", () => {
  it("labels the system actor", () => {
    expect(actorLabel("system")).toBe("System");
    expect(actorLabel(null)).toBe("System");
  });

  it("shortens a kind:uuid ref", () => {
    expect(actorLabel("user:1234567890abcdef")).toBe("User 12345678");
    expect(actorLabel("agent:abcd")).toBe("Agent abcd");
  });
});

describe("humanizeKey", () => {
  it("title-cases snake/kebab keys", () => {
    expect(humanizeKey("files_changed")).toBe("Files changed");
    expect(humanizeKey("workflow-run-id")).toBe("Workflow run id");
  });
});
