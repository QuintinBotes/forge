import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/client";
import type { RoleGrant, WorkspaceRole } from "@/lib/api/types";

import {
  describeRbacError,
  isUuid,
  roleCensus,
  roleRank,
  rollUpMembers,
  shortId,
} from "./rbac-meta";

const WS = "99999999-9999-9999-9999-999999999999";

function grant(
  principalId: string,
  role: WorkspaceRole,
  id = `${role}-${principalId}`,
): RoleGrant {
  return {
    id,
    workspace_id: WS,
    principal: { type: "user", id: principalId },
    scope: { type: "workspace", id: WS },
    role,
  };
}

describe("roleRank", () => {
  it("orders admin above member above viewer above agent-runner", () => {
    expect(roleRank("admin")).toBeLessThan(roleRank("member"));
    expect(roleRank("member")).toBeLessThan(roleRank("viewer"));
    expect(roleRank("viewer")).toBeLessThan(roleRank("agent-runner"));
  });
});

describe("shortId", () => {
  it("truncates long ids and keeps short ones intact", () => {
    expect(shortId("11111111-2222-3333-4444-555555555555")).toBe("11111111…5555");
    expect(shortId("short")).toBe("short");
  });
});

describe("isUuid", () => {
  it("accepts a canonical UUID (trimmed, case-insensitive)", () => {
    expect(isUuid("  11111111-1111-1111-1111-111111111111  ")).toBe(true);
    expect(isUuid("A1B2C3D4-1111-2222-3333-444455556666")).toBe(true);
  });
  it("rejects non-UUID input", () => {
    expect(isUuid("not-a-uuid")).toBe(false);
    expect(isUuid("")).toBe(false);
    expect(isUuid("11111111-1111-1111-1111")).toBe(false);
  });
});

describe("rollUpMembers", () => {
  it("dedupes by principal, keeping the highest-capability role + its grant", () => {
    const a = "aaaaaaaa-0000-0000-0000-000000000000";
    const b = "bbbbbbbb-0000-0000-0000-000000000000";
    const members = rollUpMembers([
      grant(b, "viewer"),
      grant(a, "member", "a-member"),
      grant(a, "admin", "a-admin"),
    ]);

    // Two unique principals.
    expect(members).toHaveLength(2);
    // Sorted by capability: admin (a) first, then viewer (b).
    expect(members[0].principal.id).toBe(a);
    expect(members[0].role).toBe("admin");
    // The primary grant carries the winning role; the other is retained.
    expect(members[0].grant.id).toBe("a-admin");
    expect(members[0].otherGrants.map((g) => g.role)).toEqual(["member"]);
    expect(members[1].role).toBe("viewer");
  });

  it("returns an empty list for no grants", () => {
    expect(rollUpMembers([])).toEqual([]);
  });
});

describe("roleCensus", () => {
  it("counts members per role in canonical order", () => {
    const members = rollUpMembers([
      grant("aaaaaaaa-0000-0000-0000-000000000000", "admin"),
      grant("bbbbbbbb-0000-0000-0000-000000000000", "member"),
      grant("cccccccc-0000-0000-0000-000000000000", "member"),
    ]);
    const census = roleCensus(members);
    expect(census.map((c) => c.role)).toEqual([
      "admin",
      "member",
      "viewer",
      "agent-runner",
    ]);
    expect(census.find((c) => c.role === "member")?.count).toBe(2);
    expect(census.find((c) => c.role === "viewer")?.count).toBe(0);
  });
});

describe("describeRbacError", () => {
  it("names the permission failure on 403", () => {
    expect(describeRbacError(new ApiError(403, "no", null))).toMatch(
      /permission/i,
    );
  });
  it("explains the last-admin lockout on a 409", () => {
    const err = new ApiError(409, "conflict", {
      detail: { error: "last_admin_lockout" },
    });
    expect(describeRbacError(err)).toMatch(/without an admin/i);
  });
  it("reports a generic conflict on other 409s", () => {
    expect(
      describeRbacError(new ApiError(409, "conflict", { detail: {} })),
    ).toMatch(/conflicts/i);
  });
  it("flags invalid input on 400/422", () => {
    expect(describeRbacError(new ApiError(422, "bad", null))).toMatch(
      /look invalid/i,
    );
  });
  it("falls back to a generic message with the action verb", () => {
    expect(describeRbacError(new Error("boom"), "revoke that member")).toBe(
      "Couldn't revoke that member. Please try again.",
    );
  });
});
