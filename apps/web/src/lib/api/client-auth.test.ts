import { beforeEach, describe, expect, it, vi } from "vitest";

import { ForgeApiClient } from "./client";
import { clearApiToken, writeApiToken } from "./session";

/** A `fetch` stub typed as `fetch`, so `mock.calls` keeps its real shape. */
function stub() {
  return vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    ),
  );
}

function authHeader(fetchImpl: ReturnType<typeof stub>): string | undefined {
  const headers = fetchImpl.mock.calls[0]?.[1]?.headers;
  return (headers as Record<string, string> | undefined)?.Authorization;
}

describe("ForgeApiClient credential resolution", () => {
  beforeEach(() => {
    clearApiToken();
  });

  it("sends no Authorization header when this browser has no key", async () => {
    const fetchImpl = stub();
    await new ForgeApiClient({ fetch: fetchImpl }).health();

    expect(authHeader(fetchImpl)).toBeUndefined();
  });

  it("picks up a key stored after the client was constructed", async () => {
    // The shared `apiClient` is a module singleton built at import time, long
    // before anyone pastes a key. Latching the token in the constructor would
    // mean signing in never took effect without a full reload.
    const fetchImpl = stub();
    const client = new ForgeApiClient({ fetch: fetchImpl });

    writeApiToken("forge_system_pasted");
    await client.health();

    expect(authHeader(fetchImpl)).toBe("Bearer forge_system_pasted");
  });

  it("exposes the same token to realtime transports", () => {
    const client = new ForgeApiClient({ fetch: stub() });
    writeApiToken("forge_system_pasted");

    // The board/collab WebSockets read `client.token` for their `?token=`
    // param; REST and WS auth must never drift apart.
    expect(client.token).toBe("forge_system_pasted");
  });

  it("stops sending the key after sign-out", async () => {
    writeApiToken("forge_system_pasted");
    const fetchImpl = stub();
    const client = new ForgeApiClient({ fetch: fetchImpl });

    clearApiToken();
    await client.health();

    expect(authHeader(fetchImpl)).toBeUndefined();
  });

  it("lets an explicitly configured token win over stored state", async () => {
    writeApiToken("forge_system_stored");
    const fetchImpl = stub();
    const client = new ForgeApiClient({
      fetch: fetchImpl,
      token: "forge_system_pinned",
    });

    await client.health();
    expect(authHeader(fetchImpl)).toBe("Bearer forge_system_pinned");
  });

  it("lets a per-call token win over everything", async () => {
    writeApiToken("forge_system_stored");
    const fetchImpl = stub();
    const client = new ForgeApiClient({ fetch: fetchImpl });

    await client.me({ token: "forge_system_candidate" });
    expect(authHeader(fetchImpl)).toBe("Bearer forge_system_candidate");
  });
});
