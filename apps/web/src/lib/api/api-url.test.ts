import { afterEach, describe, expect, it, vi } from "vitest";

import { ForgeApiClient } from "./client";
import {
  resolveApiBaseUrl,
  sameOriginApiBaseUrl,
  toAbsoluteApiBase,
} from "./api-url";

/** Point `window.location` at a fake origin for the duration of one test. */
function stubLocation(loc: {
  protocol: string;
  host: string;
  hostname: string;
  port: string;
  origin: string;
}) {
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: loc,
  });
}

const ORIGINAL_LOCATION = window.location;

afterEach(() => {
  // Restore the real global bindings first (an SSR test may have unset
  // `window`), *then* restore the original jsdom location on it.
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: ORIGINAL_LOCATION,
  });
});

describe("sameOriginApiBaseUrl", () => {
  it("derives a same-origin /api base on an https page", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    expect(sameOriginApiBaseUrl()).toBe("https://forge.example.com/api");
  });

  it("derives a same-origin /api base on an http non-localhost page", () => {
    stubLocation({
      protocol: "http:",
      host: "forge.internal:8080",
      hostname: "forge.internal",
      port: "8080",
      origin: "http://forge.internal:8080",
    });
    expect(sameOriginApiBaseUrl()).toBe("http://forge.internal:8080/api");
  });

  it("keeps the legacy dev default on the Next dev server (localhost:3000)", () => {
    // The :3000 dev web server serves no /api route — the API listens on :8000.
    stubLocation({
      protocol: "http:",
      host: "localhost:3000",
      hostname: "localhost",
      port: "3000",
      origin: "http://localhost:3000",
    });
    expect(sameOriginApiBaseUrl()).toBe("http://localhost:8000");
  });

  it("still derives same-origin /api for a full stack served on localhost via the proxy (port 80)", () => {
    // The full compose stack fronts the app with Caddy on :80/:443, where
    // same-origin /api IS routed to the API — only the :3000 dev web server is
    // the exception.
    stubLocation({
      protocol: "http:",
      host: "localhost",
      hostname: "localhost",
      port: "",
      origin: "http://localhost",
    });
    expect(sameOriginApiBaseUrl()).toBe("http://localhost/api");
  });

  it("returns the static default in SSR / non-browser contexts", () => {
    vi.stubGlobal("window", undefined);
    expect(sameOriginApiBaseUrl()).toBe("http://localhost:8000");
  });
});

describe("resolveApiBaseUrl", () => {
  it("uses an ABSOLUTE NEXT_PUBLIC_API_URL verbatim (operator override wins, byte-identical)", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    expect(resolveApiBaseUrl()).toBe("https://api.example.com");
  });

  it("resolves a RELATIVE NEXT_PUBLIC_API_URL against the page origin in the browser", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "/api");
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    expect(resolveApiBaseUrl()).toBe("https://forge.example.com/api");
  });

  it("resolves a RELATIVE NEXT_PUBLIC_API_URL against the static origin in SSR (no throw)", () => {
    vi.stubGlobal("window", undefined);
    vi.stubEnv("NEXT_PUBLIC_API_URL", "/api");
    expect(resolveApiBaseUrl()).toBe("http://localhost:8000/api");
  });

  it("derives same-origin /api when NEXT_PUBLIC_API_URL is unset (browser https)", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    expect(resolveApiBaseUrl()).toBe("https://forge.example.com/api");
  });

  it("keeps the legacy dev default when unset on the Next dev server (localhost:3000)", () => {
    stubLocation({
      protocol: "http:",
      host: "localhost:3000",
      hostname: "localhost",
      port: "3000",
      origin: "http://localhost:3000",
    });
    expect(resolveApiBaseUrl()).toBe("http://localhost:8000");
  });

  it("returns the static default when unset in SSR / non-browser contexts", () => {
    vi.stubGlobal("window", undefined);
    expect(resolveApiBaseUrl()).toBe("http://localhost:8000");
  });
});

describe("toAbsoluteApiBase", () => {
  it("returns an absolute base verbatim", () => {
    expect(toAbsoluteApiBase("https://api.example.com/v1")).toBe(
      "https://api.example.com/v1",
    );
  });

  it("resolves a relative base against the page origin in the browser", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    expect(toAbsoluteApiBase("/api")).toBe("https://forge.example.com/api");
  });

  it("resolves a relative base against the static origin in SSR", () => {
    vi.stubGlobal("window", undefined);
    expect(toAbsoluteApiBase("/api")).toBe("http://localhost:8000/api");
  });
});

describe("ForgeApiClient with a relative base URL", () => {
  it("does not throw and issues requests to an absolute same-origin URL", async () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
      origin: "https://forge.example.com",
    });
    const fetchImpl = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const client = new ForgeApiClient({
      baseUrl: "/api",
      fetch: fetchImpl as unknown as typeof fetch,
    });

    await client.health();

    const [url] = fetchImpl.mock.calls[0];
    expect(String(url)).toBe("https://forge.example.com/api/health");
  });
});
