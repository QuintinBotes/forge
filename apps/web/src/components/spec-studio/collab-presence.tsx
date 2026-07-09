"use client";

import type { CollabPeer } from "@/lib/realtime/use-spec-collab";
import { cn } from "@/lib/utils";

export interface CollabPresenceProps {
  peers: CollabPeer[];
  connected: boolean;
  synced: boolean;
}

/**
 * The live-collaboration presence bar for a CRDT-synced spec surface: a
 * connection dot plus a coloured chip per editor in the room. Cursor colours
 * come from the design-token chart ramp (`hsl(var(--chart-N))`) via
 * `CollabPeer.colorVar` — no hardcoded hex.
 */
export function CollabPresence({ peers, connected, synced }: CollabPresenceProps) {
  return (
    <div
      className="flex items-center gap-2 text-xs text-muted-foreground"
      data-testid="collab-presence"
    >
      <span
        aria-hidden
        className={cn(
          "inline-block h-2 w-2 rounded-full",
          connected ? "bg-success" : "bg-muted-foreground/40",
        )}
      />
      <span data-testid="collab-status">
        {connected ? (synced ? "Live" : "Syncing…") : "Offline"}
      </span>
      {peers.length > 0 ? (
        <ul className="flex items-center gap-1.5" aria-label="Editors in this spec">
          {peers.map((peer) => (
            <li
              key={peer.clientId}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-card/60 px-2 py-0.5"
              data-testid={`collab-peer-${peer.clientId}`}
            >
              <span
                aria-hidden
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: peer.colorVar }}
              />
              <span className={peer.isSelf ? "text-foreground" : undefined}>
                {peer.name}
                {peer.isSelf ? " (you)" : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
