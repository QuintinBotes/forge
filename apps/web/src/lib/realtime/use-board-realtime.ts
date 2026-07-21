"use client";

/**
 * Board realtime hook.
 *
 * Subscribes to the Forge realtime WebSocket and invalidates the relevant
 * TanStack Query caches when server events arrive (spec: "Live task updates,
 * run traces, approvals"). The socket is created through an injectable factory
 * so the hook is testable under jsdom (which has no `WebSocket`).
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { apiClient } from "@/lib/api/client";

import { resolveBoardWsUrl } from "./ws-url";

/** Minimal structural type for the bits of `WebSocket` this hook uses. */
export interface WebSocketLike {
  readyState: number;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  removeEventListener(type: string, listener: (event: unknown) => void): void;
  close(): void;
}

export interface BoardRealtimeEvent {
  /** Dotted event type, e.g. `task.updated`, `incident.created`. */
  type: string;
  task_id?: string;
  incident_id?: string;
  epic_id?: string;
  [key: string]: unknown;
}

export type SocketFactory = (url: string) => WebSocketLike;

export interface UseBoardRealtimeOptions {
  url?: string;
  enabled?: boolean;
  /**
   * Bearer token forwarded as the `?token=` WS auth query param (mirrors the
   * spec-collab hook). Defaults to the same token source `apiClient` uses
   * (`ForgeApiClient#token`), so board and REST auth never drift apart.
   */
  token?: string;
  /** Inject a socket (tests); defaults to the global `WebSocket`. */
  socketFactory?: SocketFactory;
  /** Optional side-channel for parsed events (toasts, trace viewer, …). */
  onEvent?: (event: BoardRealtimeEvent) => void;
}

export interface BoardRealtimeState {
  connected: boolean;
}

/**
 * Append `?token=` to a WS URL for the server's WS auth dependency (query
 * param, since WS upgrade requests can't carry an `Authorization` header).
 * String-based rather than `URL`-based so it tolerates whatever the caller
 * passes (relative test fixtures, `ws://`/`wss://`, existing query strings)
 * without a scheme-parsing edge case swallowing the token.
 */
export function withAuthToken(url: string, token?: string): string {
  if (!token) {
    return url;
  }
  const [beforeHash, hash] = url.split("#", 2);
  const separator = beforeHash.includes("?") ? "&" : "?";
  const withToken = `${beforeHash}${separator}token=${encodeURIComponent(token)}`;
  return hash !== undefined ? `${withToken}#${hash}` : withToken;
}

function defaultSocketFactory(url: string): WebSocketLike {
  if (typeof WebSocket === "undefined") {
    throw new Error("WebSocket is not available in this environment");
  }
  return new WebSocket(url) as unknown as WebSocketLike;
}

function hasData(event: unknown): event is { data: string } {
  return (
    typeof event === "object" &&
    event !== null &&
    "data" in event &&
    typeof (event as { data: unknown }).data === "string"
  );
}

/** Map an event type to the query roots that must be revalidated. */
function queryKeysForEvent(type: string): readonly unknown[][] {
  if (type.startsWith("incident")) {
    return [["incidents"]];
  }
  if (type.startsWith("epic")) {
    return [["epics"], ["tasks"]];
  }
  if (type.startsWith("run")) {
    return [["runs"]];
  }
  if (type.startsWith("approval")) {
    return [["approvals"]];
  }
  // Default (including task.*): refresh task lists/detail.
  return [["tasks"]];
}

export function useBoardRealtime(
  options: UseBoardRealtimeOptions = {},
): BoardRealtimeState {
  const {
    url = resolveBoardWsUrl(),
    enabled = true,
    token = apiClient.token,
    socketFactory,
    onEvent,
  } = options;
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const factory = socketFactory ?? defaultSocketFactory;
    let socket: WebSocketLike;
    try {
      socket = factory(withAuthToken(url, token));
    } catch {
      return;
    }

    const handleOpen = () => setConnected(true);
    const handleClose = () => setConnected(false);
    const handleMessage = (event: unknown) => {
      if (!hasData(event)) {
        return;
      }
      let payload: BoardRealtimeEvent;
      try {
        payload = JSON.parse(event.data) as BoardRealtimeEvent;
      } catch {
        return;
      }
      if (typeof payload?.type !== "string") {
        return;
      }
      onEvent?.(payload);
      for (const queryKey of queryKeysForEvent(payload.type)) {
        void queryClient.invalidateQueries({ queryKey });
      }
    };

    socket.addEventListener("open", handleOpen);
    socket.addEventListener("close", handleClose);
    socket.addEventListener("message", handleMessage);

    return () => {
      socket.removeEventListener("open", handleOpen);
      socket.removeEventListener("close", handleClose);
      socket.removeEventListener("message", handleMessage);
      socket.close();
    };
  }, [enabled, url, token, socketFactory, onEvent, queryClient]);

  return { connected };
}
