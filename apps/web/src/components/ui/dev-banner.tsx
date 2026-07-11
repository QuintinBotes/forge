"use client";

import { AlertTriangle, X } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * localStorage key gating the dismissed state. Bump the trailing version if
 * the wording changes enough that everyone should see it again.
 */
export const DEV_BANNER_DISMISSED_KEY = "forge.dev-banner.dismissed.v1";

const REPO_URL = "https://github.com/QuintinBotes/forge";
const RELEASE_READINESS_URL = `${REPO_URL}/blob/main/RELEASE_READINESS.md`;

function readDismissed(storageKey: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(storageKey) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(storageKey: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, "1");
  } catch {
    // Storage may be unavailable (private mode, quota) — the banner still
    // hides for this session, it just won't stay dismissed after reload.
  }
}

export interface DevBannerProps {
  /** Override the storage key (test isolation). */
  storageKey?: string;
}

/**
 * A persistent, dismissible notice that this Forge instance is pre-1.0 and
 * under active development — the in-app mirror of the README's status badge,
 * so nobody mistakes a self-hosted install for production-ready. Dismissal
 * is remembered per-browser via `localStorage`.
 *
 * Starts hidden and only reveals itself post-mount once it has checked
 * `localStorage`, so a returning visitor who already dismissed it never sees
 * it flash in before disappearing (SSR/first client render always agree:
 * hidden).
 */
export function DevBanner({ storageKey = DEV_BANNER_DISMISSED_KEY }: DevBannerProps = {}) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect */
    setVisible(!readDismissed(storageKey));
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [storageKey]);

  if (!visible) {
    return null;
  }

  const dismiss = () => {
    setVisible(false);
    writeDismissed(storageKey);
  };

  return (
    <div
      role="note"
      aria-label="Development status notice"
      className={cn(
        "flex items-center gap-3 border-b border-warning/40 bg-warning/10 px-4 py-2 text-sm text-warning-foreground sm:px-6",
        "motion-safe:animate-in motion-safe:fade-in motion-safe:duration-200",
      )}
    >
      <AlertTriangle aria-hidden className="h-4 w-4 shrink-0" />
      <p className="min-w-0 flex-1">
        <strong className="font-semibold">Under active development</strong> — pre-1.0, not
        production-ready. See the{" "}
        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="underline underline-offset-2 hover:text-foreground"
        >
          repo
        </a>{" "}
        and{" "}
        <a
          href={RELEASE_READINESS_URL}
          target="_blank"
          rel="noreferrer"
          className="underline underline-offset-2 hover:text-foreground"
        >
          release readiness
        </a>{" "}
        for the honest status.
      </p>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss development status notice"
        className="shrink-0 rounded p-0.5 text-warning-foreground/70 hover:text-warning-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X aria-hidden className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
