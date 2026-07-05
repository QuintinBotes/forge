import { describe, expect, it } from "vitest";

import type { Listing } from "@/lib/api/types";

import {
  filterListings,
  formatDate,
  installStatusBadge,
  isBlocked,
  kindLabel,
  needsAcknowledgement,
  shortHash,
  trustBadge,
  verificationBadge,
} from "./marketplace-meta";

function listing(over: Partial<Listing> = {}): Listing {
  return {
    id: over.id ?? "l1",
    registry_id: "r1",
    registry_slug: over.registry_slug ?? "forge-official",
    trust_level: "official",
    kind: over.kind ?? "skill_profile",
    slug: over.slug ?? "python-pro",
    name: over.name ?? "Python Pro",
    summary: over.summary ?? "An expert Python engineering profile",
    tags: over.tags ?? ["python", "backend"],
    latest_version: "1.2.0",
    license: "Apache-2.0",
    cached_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("marketplace-meta", () => {
  it("labels artifact kinds for humans", () => {
    expect(kindLabel("mcp_connector")).toBe("MCP connector");
    expect(kindLabel("skill_profile")).toBe("Skill profile");
    // Unknown kinds pass through rather than throwing.
    expect(kindLabel("something_new")).toBe("something_new");
  });

  it("maps trust levels to labels + token-only classes (never hex)", () => {
    const official = trustBadge("official");
    expect(official.label).toBe("Official");
    for (const level of ["official", "trusted", "community", "unverified"] as const) {
      expect(trustBadge(level).className).not.toMatch(/#|rgb/);
    }
    expect(trustBadge("unverified").className).toContain("text-warning");
  });

  it("classifies verification statuses (blocked vs acknowledgeable)", () => {
    expect(verificationBadge("verified").label).toBe("Verified signature");
    expect(verificationBadge("hash_mismatch").className).toContain("text-danger");

    expect(isBlocked("hash_mismatch")).toBe(true);
    expect(isBlocked("signature_invalid")).toBe(true);
    expect(isBlocked("verified")).toBe(false);
    expect(isBlocked("unsigned")).toBe(false);

    expect(needsAcknowledgement("unsigned")).toBe(true);
    expect(needsAcknowledgement("untrusted_registry")).toBe(true);
    expect(needsAcknowledgement("verified")).toBe(false);
    expect(needsAcknowledgement("hash_mismatch")).toBe(false);
  });

  it("labels installation statuses", () => {
    expect(installStatusBadge("installed").label).toBe("Installed");
    expect(installStatusBadge("update_available").label).toBe("Update available");
    expect(installStatusBadge("failed").className).toContain("text-danger");
  });

  it("abbreviates a content hash keeping its algorithm prefix", () => {
    const hex = "a".repeat(64);
    expect(shortHash(`sha256:${hex}`)).toBe("sha256:aaaaaaaa…");
    expect(shortHash("deadbeefcafe")).toBe("deadbeef…");
    expect(shortHash("")).toBe("");
  });

  it("formats provenance dates as UTC yyyy-mm-dd", () => {
    expect(formatDate("2026-03-04T12:30:00Z")).toBe("2026-03-04");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
  });

  it("filters the catalog across name, summary, slug, tags and registry", () => {
    const all = [
      listing({ id: "a", name: "Python Pro", slug: "python-pro", tags: ["python"] }),
      listing({
        id: "b",
        name: "GitHub Connector",
        slug: "github",
        kind: "mcp_connector",
        summary: "Talk to GitHub issues and PRs",
        tags: ["github", "vcs"],
        registry_slug: "acme-registry",
      }),
    ];

    expect(filterListings(all, "")).toHaveLength(2);
    expect(filterListings(all, "python").map((l) => l.id)).toEqual(["a"]);
    // Case-insensitive, matches summary text.
    expect(filterListings(all, "ISSUES").map((l) => l.id)).toEqual(["b"]);
    // Matches the registry slug.
    expect(filterListings(all, "acme").map((l) => l.id)).toEqual(["b"]);
    // Matches a tag.
    expect(filterListings(all, "vcs").map((l) => l.id)).toEqual(["b"]);
    expect(filterListings(all, "zzz")).toHaveLength(0);
  });
});
