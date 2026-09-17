/**
 * Client-side storage for the Forge API key the browser authenticates with.
 *
 * A fresh self-host has no other way in: the UI has no password login, and
 * `POST /auth/login` begins an OAuth handshake against an IdP that nobody has
 * configured yet. `make dev` therefore mints an admin key and prints it, and
 * this module is where the key the operator pastes into the Connect dialog
 * lives.
 *
 * **Where it is kept.** `sessionStorage` by default, so the credential dies
 * with the tab; "remember on this browser" promotes it to `localStorage`. That
 * default is deliberate: browser storage is scoped to an *origin*, and on
 * `http://localhost:<port>` that origin is shared with every other local app
 * that has ever used the same port — anything persisted there is readable by
 * them and vice versa. A long-lived admin token does not belong in that pool
 * unless its owner asks for it. See `docs/self-hosting/security.md`.
 *
 * Every access is wrapped: storage throws in private mode and in some embedded
 * webviews, and the app has to keep working when it does.
 */

/** Storage key. Namespaced under `forge.` like every other key this app writes. */
export const API_TOKEN_STORAGE_KEY = "forge.api.token";

/** Fired on `window` after the stored token changes, so open views can react. */
export const API_TOKEN_CHANGED_EVENT = "forge:api-token-changed";

export type TokenPersistence = "session" | "local";

function store(persistence: TokenPersistence): Storage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return persistence === "local" ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

/**
 * The stored API token, preferring the session copy.
 *
 * Returns `undefined` rather than an empty string when absent, so callers can
 * use `??` to fall through to the build-time default.
 */
export function readApiToken(): string | undefined {
  for (const persistence of ["session", "local"] as const) {
    try {
      const value = store(persistence)?.getItem(API_TOKEN_STORAGE_KEY);
      if (value) {
        return value;
      }
    } catch {
      // Unreadable storage is indistinguishable from an empty one here.
    }
  }
  return undefined;
}

/** True when the token is persisted beyond this tab. */
export function isApiTokenRemembered(): boolean {
  try {
    return Boolean(store("local")?.getItem(API_TOKEN_STORAGE_KEY));
  } catch {
    return false;
  }
}

function notifyChanged(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new CustomEvent(API_TOKEN_CHANGED_EVENT));
}

/**
 * Persist `token`, replacing whatever was stored before.
 *
 * Always clears *both* stores first so toggling "remember" can never leave a
 * stale copy behind in the other one — a signed-out token lingering in
 * `localStorage` would silently resurrect on the next read.
 */
export function writeApiToken(
  token: string,
  persistence: TokenPersistence = "session",
): void {
  clearApiToken({ notify: false });
  try {
    store(persistence)?.setItem(API_TOKEN_STORAGE_KEY, token);
  } catch {
    // Storage unavailable: the in-memory client still has the token for this
    // page load, it just will not survive a reload.
  }
  notifyChanged();
}

/** Remove the token from both stores. */
export function clearApiToken({ notify = true }: { notify?: boolean } = {}): void {
  for (const persistence of ["session", "local"] as const) {
    try {
      store(persistence)?.removeItem(API_TOKEN_STORAGE_KEY);
    } catch {
      // Nothing to do — an unwritable store holds nothing to clear.
    }
  }
  if (notify) {
    notifyChanged();
  }
}

/** Mask a token for display: `forge_system_ab…yz`. Never render the raw value. */
export function maskApiToken(token: string): string {
  if (token.length <= 12) {
    return `${token.slice(0, 4)}…`;
  }
  return `${token.slice(0, 14)}…${token.slice(-4)}`;
}
