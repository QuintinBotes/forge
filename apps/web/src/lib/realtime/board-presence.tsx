"use client";

import { cn } from "@/lib/utils";

export interface BoardConnectionIndicatorProps {
  /** `useBoardRealtime().connected`. */
  connected: boolean;
  /**
   * The WebSocket URL this indicator reflects. Shown in the tooltip while
   * disconnected so "Offline" names the endpoint that is unreachable instead of
   * leaving the reader to guess whether the stack is down, the docs sent them to
   * the wrong port, or they are simply not signed in.
   */
  url?: string;
  className?: string;
}

/**
 * Tiny connection/presence indicator for the board realtime WebSocket: a
 * status dot plus a label, mirroring the spec-collab presence bar
 * (`CollabPresence`). The board channel is a one-way server push with no
 * per-client awareness protocol, so — unlike spec collab's peer chips — this
 * only ever reflects the local socket's own connection state.
 */
export function BoardConnectionIndicator({
  connected,
  url,
  className,
}: BoardConnectionIndicatorProps) {
  const detail = connected
    ? url
      ? `Live — connected to ${url}`
      : "Live"
    : url
      ? `Offline — could not reach ${url}`
      : "Offline";

  return (
    <div
      className={cn(
        "flex items-center gap-2 text-xs text-muted-foreground",
        className,
      )}
      title={detail}
      data-testid="board-connection-indicator"
    >
      <span
        aria-hidden
        className={cn(
          "inline-block h-2 w-2 rounded-full",
          connected ? "bg-success" : "bg-muted-foreground/40",
        )}
      />
      <span data-testid="board-connection-status">
        {connected ? "Live" : "Offline"}
      </span>
      <span className="sr-only">{detail}</span>
    </div>
  );
}
