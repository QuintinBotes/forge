import { afterEach, describe, expect, it, vi } from "vitest";

import {
  THEME_INIT_SCRIPT,
  THEME_STORAGE_KEY,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
} from "./theme";

function prefersDark(matches: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}

function runInitScript() {
  new Function(THEME_INIT_SCRIPT)();
  return document.documentElement;
}

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.style.colorScheme = "";
});

describe("theme preference", () => {
  it("resolves system from the OS and keeps explicit choices", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("stores explicit choices and forgets system", () => {
    storeThemePreference("dark");
    expect(readThemePreference()).toBe("dark");
    storeThemePreference("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(readThemePreference()).toBe("system");
  });

  it("treats an unknown stored value as system", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(readThemePreference()).toBe("system");
  });
});

describe("pre-paint script", () => {
  it("applies a stored dark choice even on a light OS", () => {
    prefersDark(false);
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    const root = runInitScript();
    expect(root).toHaveClass("dark");
    expect(root.style.colorScheme).toBe("dark");
  });

  it("follows a dark OS when nothing is stored", () => {
    prefersDark(true);
    expect(runInitScript()).toHaveClass("dark");
  });

  it("keeps a stored light choice on a dark OS", () => {
    prefersDark(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    const root = runInitScript();
    expect(root).not.toHaveClass("dark");
    expect(root.style.colorScheme).toBe("light");
  });
});
