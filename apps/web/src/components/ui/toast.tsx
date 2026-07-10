"use client";

import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { useSyncExternalStore } from "react";

import { cn } from "@/lib/utils";

export type ToastVariant = "success" | "error" | "info";

export interface ToastItem {
  id: string;
  message: string;
  variant: ToastVariant;
}

let items: ToastItem[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

/** `useSyncExternalStore` subscribe: register a no-arg "something changed" callback. */
function subscribe(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  return () => listeners.delete(onStoreChange);
}

function getSnapshot(): ToastItem[] {
  return items;
}

function dismissToast(id: string) {
  items = items.filter((item) => item.id !== id);
  emit();
}

function pushToast(message: string, variant: ToastVariant, durationMs: number): string {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  items = [...items, { id, message, variant }];
  emit();
  if (typeof window !== "undefined") {
    window.setTimeout(() => dismissToast(id), durationMs);
  }
  return id;
}

/**
 * Fire a toast confirming the result of an action — the "Published"/"Installed"
 * copy the UX checklist asks every mutating action to end with. Safe to call
 * from anywhere (dialogs, hooks, event handlers): it is a no-op data push until
 * the single {@link Toaster} the app shell mounts is on screen to render it, so
 * it never throws in tests that render a component in isolation.
 */
export function toast(message: string, variant: ToastVariant = "success", durationMs = 4000): string {
  return pushToast(message, variant, durationMs);
}

toast.success = (message: string, durationMs = 4000) => toast(message, "success", durationMs);
toast.error = (message: string, durationMs = 6000) => toast(message, "error", durationMs);
toast.info = (message: string, durationMs = 4000) => toast(message, "info", durationMs);

const VARIANT_ICON: Record<ToastVariant, typeof CheckCircle2> = {
  success: CheckCircle2,
  error: AlertTriangle,
  info: Info,
};

const VARIANT_CLASS: Record<ToastVariant, string> = {
  success: "border-success/40 bg-success/10 text-success",
  error: "border-danger/40 bg-danger/10 text-danger",
  info: "border-border bg-card text-foreground",
};

/**
 * The single toast stack, mounted once in the root `Providers`. A fixed,
 * `aria-live="polite"` region so action confirmations are announced without
 * stealing focus — token-driven, colour-blind safe (icon + text, not colour
 * alone), and animates in only under `motion-safe` (a no-op transition when
 * the visitor prefers reduced motion).
 */
export function Toaster() {
  const visible = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  if (visible.length === 0) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4 sm:items-end sm:px-6"
    >
      {visible.map((item) => {
        const Icon = VARIANT_ICON[item.variant];
        return (
          <div
            key={item.id}
            data-testid="toast"
            data-variant={item.variant}
            className={cn(
              "pointer-events-auto flex w-full max-w-sm items-start gap-2 rounded-lg border bg-card px-3.5 py-2.5 text-sm text-foreground shadow-lg",
              "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2 motion-safe:duration-200",
              VARIANT_CLASS[item.variant],
            )}
          >
            <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span className="flex-1">{item.message}</span>
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => dismissToast(item.id)}
              className="shrink-0 rounded p-0.5 text-current/70 hover:text-current focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );
}
