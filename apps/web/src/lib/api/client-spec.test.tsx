import { describe, expect, it, vi } from "vitest";

import { ForgeApiClient } from "./client";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function text(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

/**
 * Covers the ss-endpoints spec-engine client surface: creating a spec, then
 * editing it via both first-class formats (spec.md and manifest.yaml), plus
 * the lifecycle actions and constitution read.
 */
describe("ForgeApiClient spec-engine surface", () => {
  it("createSpec posts to /spec/specs", async () => {
    const fetchImpl = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(json({ id: "SPEC-1", name: "Customer search", status: "draft" })),
    );
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const manifest = await client.createSpec({
      epic_id: "epic-1",
      name: "Customer search",
      requirements: [{ id: "R1", text: "Search customers by name" }],
    });

    expect(manifest.name).toBe("Customer search");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(String(url)).toContain("/spec/specs");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toMatchObject({ name: "Customer search" });
  });

  it("reads and writes a spec via its spec.md prose serialization", async () => {
    const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/markdown") && init?.method === "GET") {
        return Promise.resolve(text("---\nid: SPEC-1\n---\n\n## Goal\n\nCustomer search\n"));
      }
      if (url.includes("/markdown") && init?.method === "PUT") {
        return Promise.resolve(json({ id: "SPEC-1", name: "Customer search", status: "draft" }));
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const md = await client.getSpecMarkdown("spec-uuid-1");
    expect(md).toContain("Customer search");

    const updated = await client.putSpecMarkdown("spec-uuid-1", md);
    expect(updated.id).toBe("SPEC-1");
    const [, putInit] = fetchImpl.mock.calls[1];
    expect(JSON.parse(putInit?.body as string)).toEqual({ content: md });
  });

  it("creates and edits a spec via its manifest.yaml serialization", async () => {
    const yamlText = "id: SPEC-99\nname: Billing v2\nstatus: draft\n";
    const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/manifest") && init?.method === "PUT") {
        return Promise.resolve(json({ id: "SPEC-99", name: "Billing v2", status: "draft" }));
      }
      if (url.includes("/manifest")) {
        return Promise.resolve(text(yamlText));
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const created = await client.putSpecManifestYaml("spec-uuid-99", yamlText);
    expect(created.name).toBe("Billing v2");

    const yaml = await client.getSpecManifestYaml("spec-uuid-99");
    expect(yaml).toContain("Billing v2");
  });

  it("putSpecManifest persists the full manifest (Guided mode save path)", async () => {
    const fetchImpl = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("PUT");
      return Promise.resolve(json({ id: "SPEC-1", name: "Renamed", status: "draft" }));
    });
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const updated = await client.putSpecManifest("spec-uuid-1", {
      id: "SPEC-1",
      name: "Renamed",
      status: "draft",
    });

    expect(updated.name).toBe("Renamed");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(String(url)).toContain("/spec/specs/spec-uuid-1");
    expect(JSON.parse(init?.body as string)).toMatchObject({ name: "Renamed" });
  });

  it("drives clarify -> plan -> approve -> generateTasks -> validateTask", async () => {
    const fetchImpl = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/clarify")) {
        return Promise.resolve(json({ id: "SPEC-1", name: "x", status: "clarifying" }));
      }
      if (url.includes("/plan")) {
        return Promise.resolve(json({ id: "SPEC-1", name: "x", status: "clarifying" }));
      }
      if (url.includes("/approve")) {
        return Promise.resolve(json({ id: "SPEC-1", name: "x", status: "approved" }));
      }
      if (url.includes("/validate")) {
        return Promise.resolve(json({ task_id: "t1", passed: true }));
      }
      if (url.includes("/tasks")) {
        return Promise.resolve(json([{ id: "t1", title: "Implement", status: "todo" }]));
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    expect((await client.clarifySpec("spec-1")).status).toBe("clarifying");
    expect((await client.planSpec("spec-1")).status).toBe("clarifying");
    expect((await client.approveSpec("spec-1")).status).toBe("approved");
    const tasks = await client.generateTasks("spec-1");
    expect(tasks).toHaveLength(1);
    const report = await client.validateTask("t1");
    expect(report.passed).toBe(true);
  });

  it("rejectSpec and requestSpecChanges post the note to the review endpoints", async () => {
    const fetchImpl = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/request-changes")) {
        return Promise.resolve(
          json({ id: "SPEC-1", name: "x", status: "changes_requested", review_note: "tighten" }),
        );
      }
      if (url.includes("/reject")) {
        return Promise.resolve(
          json({ id: "SPEC-1", name: "x", status: "rejected", review_note: "nope" }),
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const rejected = await client.rejectSpec("spec-1", "nope");
    expect(rejected.status).toBe("rejected");
    expect(rejected.review_note).toBe("nope");

    const changed = await client.requestSpecChanges("spec-1", "tighten");
    expect(changed.status).toBe("changes_requested");

    const [rejectUrl, rejectInit] = fetchImpl.mock.calls[0];
    expect(String(rejectUrl)).toContain("/spec/specs/spec-1/reject");
    expect(rejectInit?.method).toBe("POST");
    expect(JSON.parse(rejectInit?.body as string)).toEqual({ note: "nope" });

    const [changesUrl, changesInit] = fetchImpl.mock.calls[1];
    expect(String(changesUrl)).toContain("/spec/specs/spec-1/request-changes");
    expect(JSON.parse(changesInit?.body as string)).toEqual({ note: "tighten" });
  });

  it("getConstitution reads /spec/constitution/{project_id}", async () => {
    const fetchImpl = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(json({ project_id: "proj-1", principles: ["Ship small"] })),
    );
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const constitution = await client.getConstitution("proj-1");

    expect(constitution.project_id).toBe("proj-1");
    const [url] = fetchImpl.mock.calls[0];
    expect(String(url)).toContain("/spec/constitution/proj-1");
  });

  it("importSpec posts content (+ optional source_format) to /spec/import", async () => {
    const fetchImpl = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(
        json({
          source_format: "markdown",
          spec_md: "---\nid: SPEC-IMPORT\n---\n\n## Goal\n\nImported feature\n",
          manifest: { id: "SPEC-IMPORT", name: "Imported feature" },
          normalized: true,
        }),
      ),
    );
    const client = new ForgeApiClient({ fetch: fetchImpl as unknown as typeof fetch });

    const result = await client.importSpec({
      content: "# Imported feature\n",
      source_format: "markdown",
    });

    expect(result.source_format).toBe("markdown");
    expect(result.normalized).toBe(true);
    expect(result.manifest?.name).toBe("Imported feature");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(String(url)).toContain("/spec/import");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      content: "# Imported feature\n",
      source_format: "markdown",
    });
  });
});
