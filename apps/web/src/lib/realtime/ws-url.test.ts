import { afterEach, describe, expect, it, vi } from "vitest";

import {
  resolveBoardWsUrl,
  resolveSpecWsBaseUrl,
  sameOriginWsUrl,
} from "./ws-url";

/** Point `window.location` at a fake origin for the duration of one test. */
function stubLocation(loc: {
  protocol: string;
  host: string;
  hostname: string;
  port: string;
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

describe("sameOriginWsUrl", () => {
  it("derives a wss same-origin URL on an https page", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
    });
    expect(sameOriginWsUrl("/ws")).toBe("wss://forge.example.com/ws");
  });

  it("derives a ws same-origin URL on an http non-localhost page", () => {
    stubLocation({
      protocol: "http:",
      host: "forge.internal:8080",
      hostname: "forge.internal",
      port: "8080",
    });
    expect(sameOriginWsUrl("/ws")).toBe("ws://forge.internal:8080/ws");
  });

  it("keeps the legacy dev default on the Next dev server (localhost:3000)", () => {
    stubLocation({
      protocol: "http:",
      host: "localhost:3000",
      hostname: "localhost",
      port: "3000",
    });
    expect(sameOriginWsUrl("/ws")).toBe("ws://localhost:8000/ws");
  });

  it("still derives same-origin for a full stack served on localhost via the proxy (port 80)", () => {
    // The full compose stack fronts the app with Caddy on :80/:443, where
    // same-origin /ws IS routed to the API — only the :3000 dev web server is
    // the exception.
    stubLocation({
      protocol: "http:",
      host: "localhost",
      hostname: "localhost",
      port: "",
    });
    expect(sameOriginWsUrl("/ws")).toBe("ws://localhost/ws");
  });

  it("returns the static default in SSR / non-browser contexts", () => {
    vi.stubGlobal("window", undefined);
    expect(sameOriginWsUrl("/ws")).toBe("ws://localhost:8000/ws");
  });
});

describe("resolveBoardWsUrl", () => {
  it("uses NEXT_PUBLIC_WS_URL verbatim when set (operator override wins)", () => {
    vi.stubEnv("NEXT_PUBLIC_WS_URL", "wss://ws.example.com/custom/ws");
    expect(resolveBoardWsUrl()).toBe("wss://ws.example.com/custom/ws");
  });

  it("derives same-origin /ws when NEXT_PUBLIC_WS_URL is unset", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
    });
    expect(resolveBoardWsUrl()).toBe("wss://forge.example.com/ws");
  });
});

describe("resolveSpecWsBaseUrl", () => {
  it("uses NEXT_PUBLIC_SPEC_WS_URL verbatim when set", () => {
    vi.stubEnv("NEXT_PUBLIC_SPEC_WS_URL", "wss://ws.example.com/spec-base");
    expect(resolveSpecWsBaseUrl()).toBe("wss://ws.example.com/spec-base");
  });

  it("derives /ws/spec from NEXT_PUBLIC_WS_URL when only it is set (byte-identical to legacy)", () => {
    vi.stubEnv("NEXT_PUBLIC_WS_URL", "wss://forge.example.com/ws");
    expect(resolveSpecWsBaseUrl()).toBe("wss://forge.example.com/ws/spec");
  });

  it("derives a same-origin /ws/spec base on an https page when no override is set", () => {
    stubLocation({
      protocol: "https:",
      host: "forge.example.com",
      hostname: "forge.example.com",
      port: "",
    });
    expect(resolveSpecWsBaseUrl()).toBe("wss://forge.example.com/ws/spec");
  });

  it("keeps the legacy dev base on the Next dev server (localhost:3000)", () => {
    stubLocation({
      protocol: "http:",
      host: "localhost:3000",
      hostname: "localhost",
      port: "3000",
    });
    expect(resolveSpecWsBaseUrl()).toBe("ws://localhost:8000/ws/spec");
  });
});
