import { describe, expect, it, vi } from "vitest";

import { ForgeApiClient } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeClient(response: Response) {
  const fetchImpl = vi.fn(() => Promise.resolve(response));
  const client = new ForgeApiClient({
    baseUrl: "http://api.test",
    fetch: fetchImpl as unknown as typeof fetch,
  });
  return { client, fetchImpl };
}

describe("ForgeApiClient — attestations", () => {
  it("maps a 404 from the by-approval endpoint to null (confirmed absence)", async () => {
    const { client, fetchImpl } = makeClient(
      jsonResponse(404, { detail: "no attestation recorded for approval a1" }),
    );

    await expect(client.getApprovalAttestation("a1")).resolves.toBeNull();
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://api.test/approvals/a1/attestation",
      expect.anything(),
    );
  });

  it("still throws for non-404 failures (an error is not absence)", async () => {
    const { client } = makeClient(jsonResponse(500, { detail: "boom" }));

    await expect(client.getApprovalAttestation("a1")).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
    });
  });

  it("requests the workspace attestation page with its query params", async () => {
    const { client, fetchImpl } = makeClient(
      jsonResponse(200, { items: [], limit: 25, offset: 50 }),
    );

    const page = await client.listAttestations({ limit: 25, offset: 50 });
    expect(page).toEqual({ items: [], limit: 25, offset: 50 });
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://api.test/attestations?limit=25&offset=50",
      expect.anything(),
    );
  });
});
