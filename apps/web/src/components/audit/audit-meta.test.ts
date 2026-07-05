import { describe, expect, it } from "vitest";

import {
  REDACTED_PLACEHOLDER,
  absoluteTime,
  actionLabel,
  actionNamespace,
  actorDisplay,
  isSecretKey,
  looksLikeSecretValue,
  outcomeMeta,
  presetToFrom,
  redactJson,
  relativeTime,
  severityMeta,
  shortId,
} from "./audit-meta";

describe("secret redaction", () => {
  it("flags credential-looking key names", () => {
    for (const key of [
      "password",
      "api_key",
      "apiKey",
      "authorization",
      "client_secret",
      "access_token",
      "private_key",
      "session_id",
    ]) {
      expect(isSecretKey(key)).toBe(true);
    }
    expect(isSecretKey("target_id")).toBe(false);
    expect(isSecretKey("status")).toBe(false);
  });

  it("flags credential-looking values", () => {
    expect(looksLikeSecretValue("Bearer abc.def.ghi")).toBe(true);
    expect(looksLikeSecretValue("sk-live-0123456789")).toBe(true);
    expect(looksLikeSecretValue("ghp_0123456789abcdef")).toBe(true);
    // A long spaceless JWT-shaped blob.
    expect(
      looksLikeSecretValue("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcd"),
    ).toBe(true);
    // Not secrets: prose, short ids, paths.
    expect(looksLikeSecretValue("updated the task title")).toBe(false);
    expect(looksLikeSecretValue("in_progress")).toBe(false);
    expect(looksLikeSecretValue("/var/log/forge/audit.ndjson")).toBe(false);
  });

  it("deep-redacts secret keys and values while preserving structure", () => {
    const payload = {
      status: "ok",
      api_key: "super-secret-value",
      nested: {
        authorization: "Bearer xyz",
        note: "rotated key",
      },
      headers: [{ name: "X-Token", token: "abcdef" }],
      raw: "eyJhbGciOiJIUzI1NiJ9.payloadpayloadpayload.signaturesignature",
    };

    const redacted = redactJson(payload) as Record<string, unknown>;

    expect(redacted.status).toBe("ok");
    expect(redacted.api_key).toBe(REDACTED_PLACEHOLDER);
    expect((redacted.nested as Record<string, unknown>).authorization).toBe(
      REDACTED_PLACEHOLDER,
    );
    expect((redacted.nested as Record<string, unknown>).note).toBe("rotated key");
    const headers = redacted.headers as Record<string, unknown>[];
    expect(headers[0].name).toBe("X-Token");
    expect(headers[0].token).toBe(REDACTED_PLACEHOLDER);
    // Value-level heuristic catches an unlabelled token.
    expect(redacted.raw).toBe(REDACTED_PLACEHOLDER);
  });

  it("does not mutate the original object", () => {
    const original = { password: "hunter2" };
    redactJson(original);
    expect(original.password).toBe("hunter2");
  });
});

describe("presentation metadata", () => {
  it("maps outcomes and severities onto tokens", () => {
    expect(outcomeMeta("success").label).toBe("Success");
    expect(outcomeMeta("blocked").badgeClass).toContain("danger");
    expect(outcomeMeta("weird").label).toBe("Weird"); // humanized fallback

    expect(severityMeta("critical").weight).toBeGreaterThan(
      severityMeta("info").weight,
    );
    expect(severityMeta("unknown")).toEqual(severityMeta("info")); // safe default
  });

  it("labels and namespaces dotted actions", () => {
    expect(actionLabel("policy.tool_denied")).toBe("Tool denied");
    expect(actionNamespace("mcp.tool_call")).toBe("mcp");
    expect(actionNamespace("bareaction")).toBe("core");
  });

  it("derives an actor label, preferring the durable snapshot", () => {
    expect(
      actorDisplay({ actor_type: "user", actor_label: "alice@forge.dev" }),
    ).toBe("alice@forge.dev");
    expect(
      actorDisplay({ actor_type: "agent_runner", actor_id: "1234567890abcdef" }),
    ).toBe("Agent 12345678");
    expect(actorDisplay({ actor_type: "system" })).toBe("System");
  });

  it("shortens ids and formats time", () => {
    expect(shortId("1234567890")).toBe("12345678");
    expect(relativeTime(null)).toBe("—");
    expect(relativeTime("2026-07-05T11:00:00Z", Date.parse("2026-07-05T11:00:30Z"))).toBe(
      "just now",
    );
    expect(absoluteTime("2026-07-05T11:00:03Z")).toBe("2026-07-05 11:00:03 UTC");
  });

  it("resolves time-range presets to a lower bound", () => {
    const now = Date.parse("2026-07-05T12:00:00Z");
    expect(presetToFrom("all", now)).toBeUndefined();
    expect(presetToFrom("24h", now)).toBe("2026-07-04T12:00:00.000Z");
  });
});
