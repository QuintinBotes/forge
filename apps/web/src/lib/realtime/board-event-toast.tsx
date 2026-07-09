"use client";

import { useCallback, useRef, useState } from "react";

import { cn } from "@/lib/utils";

import type { BoardRealtimeEvent } from "./use-board-realtime";

export interface BoardToast {
  id: string;
  message: string;
}

/** How long a board-event toast stays on screen before auto-dismissing. */
const DEFAULT_TOAST_TTL_MS = 5_000;
/** Cap the stack so a burst of events doesn't flood the corner of the screen. */
const MAX_TOASTS = 3;

/**
 * Turn a dotted realtime event type into a short, human-readable toast
 * message, e.g. `task.updated` → "Task updated".
 */
export function describeBoardEvent(event: BoardRealtimeEvent): string {
  const label = event.type
    .split(".")
    .join(" ")
    .replace(/_/g, " ")
    .trim();
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : "Board update";
}

export interface UseBoardEventToastsOptions {
  ttlMs?: number;
  maxToasts?: number;
}

export interface BoardEventToastsState {
  toasts: BoardToast[];
  /** Feed a parsed realtime event; wire this up as `useBoardRealtime`'s `onEvent`. */
  notify: (event: BoardRealtimeEvent) => void;
  dismiss: (id: string) => void;
}

let toastSeq = 0;

/**
 * Local (non-global) toast queue driven by board realtime events. Kept
 * self-contained to this feature rather than a repo-wide toaster — there is
 * no shared toast primitive in the design system yet.
 */
export function useBoardEventToasts(
  options: UseBoardEventToastsOptions = {},
): BoardEventToastsState {
  const { ttlMs = DEFAULT_TOAST_TTL_MS, maxToasts = MAX_TOASTS } = options;
  const [toasts, setToasts] = useState<BoardToast[]>([]);
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback(
    (event: BoardRealtimeEvent) => {
      toastSeq += 1;
      const id = `board-toast-${toastSeq}`;
      setToasts((prev) => [...prev, { id, message: describeBoardEvent(event) }].slice(-maxToasts));
      const timer = setTimeout(() => dismiss(id), ttlMs);
      timersRef.current.set(id, timer);
    },
    [dismiss, maxToasts, ttlMs],
  );

  return { toasts, notify, dismiss };
}

export interface BoardEventToastViewportProps {
  toasts: BoardToast[];
  onDismiss: (id: string) => void;
  className?: string;
}

/** Fixed-position stack rendering `useBoardEventToasts`' current queue. */
export function BoardEventToastViewport({
  toasts,
  onDismiss,
  className,
}: BoardEventToastViewportProps) {
  if (toasts.length === 0) {
    return null;
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2",
        className,
      )}
      data-testid="board-event-toasts"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className="pointer-events-auto flex items-center gap-3 rounded-md border border-border bg-card px-3 py-2 text-xs text-foreground shadow-lg"
          data-testid="board-event-toast"
        >
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Dismiss notification"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
