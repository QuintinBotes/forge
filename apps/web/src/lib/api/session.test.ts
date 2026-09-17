import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  API_TOKEN_CHANGED_EVENT,
  API_TOKEN_STORAGE_KEY,
  clearApiToken,
  isApiTokenRemembered,
  maskApiToken,
  readApiToken,
  writeApiToken,
} from "./session";

describe("API token storage", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it("is empty before anything is stored", () => {
    expect(readApiToken()).toBeUndefined();
  });

  it("defaults to sessionStorage so the credential dies with the tab", () => {
    writeApiToken("forge_system_abc");

    expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe("forge_system_abc");
    expect(window.localStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
    expect(isApiTokenRemembered()).toBe(false);
  });

  it("persists to localStorage only when asked", () => {
    writeApiToken("forge_system_abc", "local");

    expect(window.localStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe("forge_system_abc");
    expect(isApiTokenRemembered()).toBe(true);
    expect(readApiToken()).toBe("forge_system_abc");
  });

  it("never leaves a stale copy behind when persistence changes", () => {
    // The dangerous case: a remembered token silently outliving a later
    // tab-scoped sign-in and resurrecting on the next read.
    writeApiToken("forge_system_old", "local");
    writeApiToken("forge_system_new", "session");

    expect(window.localStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
    expect(readApiToken()).toBe("forge_system_new");
  });

  it("clears both stores on sign-out", () => {
    writeApiToken("forge_system_abc", "local");
    clearApiToken();

    expect(readApiToken()).toBeUndefined();
    expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("namespaces its key under forge. so a shared origin cannot collide", () => {
    expect(API_TOKEN_STORAGE_KEY.startsWith("forge.")).toBe(true);
  });

  it("announces every change so open views can react", () => {
    const listener = vi.fn();
    window.addEventListener(API_TOKEN_CHANGED_EVENT, listener);

    writeApiToken("forge_system_abc");
    clearApiToken();

    expect(listener).toHaveBeenCalledTimes(2);
    window.removeEventListener(API_TOKEN_CHANGED_EVENT, listener);
  });

  it("survives storage throwing (private mode, blocked site data)", () => {
    const spy = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("denied");
      });

    expect(() => readApiToken()).not.toThrow();
    expect(readApiToken()).toBeUndefined();
    spy.mockRestore();
  });

  it("masks a token for display without revealing it", () => {
    const token = "forge_system_SUPERSECRETVALUE1234";
    const masked = maskApiToken(token);

    expect(masked).not.toContain("SUPERSECRETVALUE");
    expect(masked).toContain("…");
  });
});
