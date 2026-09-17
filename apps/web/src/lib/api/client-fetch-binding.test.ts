import { afterEach, describe, expect, it } from "vitest";

import { ForgeApiClient } from "./client";

/**
 * `ForgeApiClient` keeps `fetch` on the instance and calls `this.fetchImpl(...)`.
 * Stored unbound, that invokes `fetch` with the *client* as its receiver, and a
 * real browser refuses:
 *
 *   TypeError: Failed to execute 'fetch' on 'Window': Illegal invocation
 *
 * Every request from the browser fails. jsdom's `fetch` does not enforce its
 * receiver, so no ordinary test can observe this — the assertion has to be about
 * the receiver itself.
 */
describe("fetch receiver", () => {
  const original = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = original;
  });

  it("never calls the global fetch with the client as its receiver", async () => {
    const receivers: unknown[] = [];
    globalThis.fetch = function (this: unknown) {
      receivers.push(this);
      return Promise.resolve(
        new Response("{}", { headers: { "content-type": "application/json" } }),
      );
    } as unknown as typeof fetch;

    // Constructed AFTER the stub is installed, mirroring the module singleton
    // that picks up whatever `globalThis.fetch` is at import time.
    const client = new ForgeApiClient();
    await client.health();

    expect(receivers).toHaveLength(1);
    expect(receivers[0]).not.toBe(client);
    expect(receivers[0]).toBe(globalThis);
  });

  it("still honours an explicitly injected fetch", async () => {
    let called = false;
    const client = new ForgeApiClient({
      fetch: (() => {
        called = true;
        return Promise.resolve(
          new Response("{}", { headers: { "content-type": "application/json" } }),
        );
      }) as unknown as typeof fetch,
    });

    await client.health();
    expect(called).toBe(true);
  });
});
