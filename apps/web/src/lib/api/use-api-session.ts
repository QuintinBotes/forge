"use client";

/**
 * React binding over the stored API token ({@link readApiToken} & friends).
 *
 * Holds the current token in state so components re-render when it changes,
 * mirrors every change into browser storage, and clears the TanStack Query
 * cache on each transition — a cache populated under one principal must never
 * be shown to the next one.
 */

import { QueryClientContext } from "@tanstack/react-query";
import { useCallback, useContext, useEffect, useState } from "react";

import {
  API_TOKEN_CHANGED_EVENT,
  clearApiToken,
  isApiTokenRemembered,
  readApiToken,
  writeApiToken,
  type TokenPersistence,
} from "./session";

export interface ApiSession {
  /** The active token, or `undefined` when this browser has none. */
  token: string | undefined;
  /** True once the first post-mount storage read has happened. */
  ready: boolean;
  /** True when the token outlives this tab (`localStorage`). */
  remembered: boolean;
  signIn: (token: string, persistence?: TokenPersistence) => void;
  signOut: () => void;
}

export function useApiSession(): ApiSession {
  // Always start empty so the server render and the first client render agree;
  // storage is unreachable during SSR, and disagreeing here is a hydration
  // mismatch. The effect below fills it in immediately after mount.
  const [token, setToken] = useState<string | undefined>(undefined);
  const [remembered, setRemembered] = useState(false);
  const [ready, setReady] = useState(false);
  // Read the context directly rather than via `useQueryClient`, which throws
  // when no provider is mounted. The session control lives in the app shell,
  // and the shell is rendered bare in component tests; losing the whole chrome
  // to a missing provider would be a poor trade for a cache eviction.
  const queryClient = useContext(QueryClientContext);

  useEffect(() => {
    const sync = () => {
      setToken(readApiToken());
      setRemembered(isApiTokenRemembered());
      setReady(true);
    };
    sync();
    // `storage` catches a sign-in performed in another tab; the custom event
    // catches one performed in this tab (browsers do not fire `storage` at the
    // window that wrote the value).
    window.addEventListener(API_TOKEN_CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(API_TOKEN_CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const signIn = useCallback(
    (next: string, persistence: TokenPersistence = "session") => {
      writeApiToken(next.trim(), persistence);
      // Drop everything fetched anonymously (or as someone else) so no stale
      // 401-shaped cache entry survives the transition.
      queryClient?.clear();
    },
    [queryClient],
  );

  const signOut = useCallback(() => {
    clearApiToken();
    queryClient?.clear();
  }, [queryClient]);

  return { token, ready, remembered, signIn, signOut };
}
