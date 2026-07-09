"use client";

import { useCallback } from "react";

import { BoardEventToastViewport, useBoardEventToasts } from "./board-event-toast";
import { BoardConnectionIndicator } from "./board-presence";
import {
  useBoardRealtime,
  type BoardRealtimeEvent,
  type SocketFactory,
} from "./use-board-realtime";

export interface BoardRealtimeStatusProps {
  enabled?: boolean;
  url?: string;
  token?: string;
  /** Inject a socket (tests); defaults to the global `WebSocket`. */
  socketFactory?: SocketFactory;
  /** Extra side-channel for parsed events (e.g. feeding a run-trace viewer). */
  onEvent?: (event: BoardRealtimeEvent) => void;
  className?: string;
}

/**
 * Drop-in board realtime widget: owns the `useBoardRealtime` subscription
 * (auth token threaded through), renders a small connection/presence
 * indicator, and surfaces every incoming event as a toast. Replaces a bare
 * `useBoardRealtime(...)` call wherever the board mounts — one subscription,
 * visible connection state, and notifications for free.
 */
export function BoardRealtimeStatus({
  enabled,
  url,
  token,
  socketFactory,
  onEvent,
  className,
}: BoardRealtimeStatusProps) {
  const { toasts, notify, dismiss } = useBoardEventToasts();

  // Stable identity across renders (as long as the caller's `onEvent` is
  // stable too) so it doesn't force `useBoardRealtime` to tear down and
  // reopen the socket on every render.
  const handleEvent = useCallback(
    (event: BoardRealtimeEvent) => {
      notify(event);
      onEvent?.(event);
    },
    [notify, onEvent],
  );

  const { connected } = useBoardRealtime({
    enabled,
    url,
    token,
    socketFactory,
    onEvent: handleEvent,
  });

  return (
    <>
      <BoardConnectionIndicator connected={connected} className={className} />
      <BoardEventToastViewport toasts={toasts} onDismiss={dismiss} />
    </>
  );
}
