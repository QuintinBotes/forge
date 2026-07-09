"use client";

import { cn } from "@/lib/utils";

export interface BoardConnectionIndicatorProps {
  /** `useBoardRealtime().connected`. */
  connected: boolean;
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
  className,
}: BoardConnectionIndicatorProps) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 text-xs text-muted-foreground",
        className,
      )}
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
    </div>
  );
}
