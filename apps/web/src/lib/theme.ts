// Light / dark / system theme. The palettes live in globals.css (`:root` and
// `.dark`); this module only decides whether `<html>` carries `.dark`.

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "forge-theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

// Used only when storage is blocked, so the choice still holds for this page.
let unstoredPreference: ThemePreference = "system";
const listeners = new Set<() => void>();

/** The stored choice; anything else (or nothing stored) means "system". */
export function readThemePreference(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return unstoredPreference;
  }
}

export function storeThemePreference(preference: ThemePreference): void {
  unstoredPreference = preference;
  try {
    if (preference === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    // Blocked storage: `unstoredPreference` carries it.
  }
  listeners.forEach((listener) => listener());
}

/** For `useSyncExternalStore`: this tab's changes and other tabs' (`storage`). */
export function subscribeThemePreference(onChange: () => void): () => void {
  listeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

export function systemPrefersDark(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia(DARK_QUERY).matches
  );
}

export function resolveTheme(
  preference: ThemePreference,
  prefersDark: boolean,
): ResolvedTheme {
  if (preference === "system") {
    return prefersDark ? "dark" : "light";
  }
  return preference;
}

export function applyTheme(theme: ResolvedTheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/** Calls `onChange` whenever the OS switches between light and dark. */
export function watchSystemTheme(
  onChange: (prefersDark: boolean) => void,
): () => void {
  if (typeof window.matchMedia !== "function") {
    return () => {};
  }
  const query = window.matchMedia(DARK_QUERY);
  const listener = (event: MediaQueryListEvent) => onChange(event.matches);
  query.addEventListener("change", listener);
  return () => query.removeEventListener("change", listener);
}

/**
 * Runs in `<head>` before first paint so a dark-theme reader never sees a flash
 * of the light palette. It repeats the logic above because it cannot import.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var p=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});var d=p==="dark"||(p!=="light"&&window.matchMedia(${JSON.stringify(
  DARK_QUERY,
)}).matches);var r=document.documentElement;r.classList.toggle("dark",d);r.style.colorScheme=d?"dark":"light"}catch(e){}})();`;
