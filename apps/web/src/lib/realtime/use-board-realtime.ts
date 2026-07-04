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
  /** Inject a socket (tests); defaults to the global `WebSocket`. */
  socketFactory?: SocketFactory;
  /** Optional side-channel for parsed events (toasts, trace viewer, …). */
  onEvent?: (event: BoardRealtimeEvent) => void;
}

export interface BoardRealtimeState {
  connected: boolean;
}

export const DEFAULT_WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

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
  // Default (including task.*): refresh task lists/detail.
  return [["tasks"]];
}

export function useBoardRealtime(
  options: UseBoardRealtimeOptions = {},
): BoardRealtimeState {
  const { url = DEFAULT_WS_URL, enabled = true, socketFactory, onEvent } =
    options;
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const factory = socketFactory ?? defaultSocketFactory;
    let socket: WebSocketLike;
    try {
      socket = factory(url);
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
  }, [enabled, url, socketFactory, onEvent, queryClient]);

  return { connected };
}
