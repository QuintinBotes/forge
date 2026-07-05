"use client";

import { Check, Copy } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { cn } from "@/lib/utils";

export interface CopyFieldProps {
  /** What this value is, in the admin's words (e.g. "ACS URL"). */
  label: string;
  value: string;
  /** Render the value as a scrollable block (e.g. a PEM certificate). */
  multiline?: boolean;
  /** Optional hint shown under the label. */
  hint?: string;
  className?: string;
}

/**
 * A labelled, read-only technical value (entity ID, ACS URL, certificate) with a
 * one-click copy. These are the strings an admin pastes into their IdP, so they
 * are set in the mono voice and never truncated silently — the block scrolls.
 */
export function CopyField({
  label,
  value,
  multiline = false,
  hint,
  className,
}: CopyFieldProps) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const onCopy = useCallback(() => {
    const done = () => {
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    };
    try {
      const clip = navigator.clipboard;
      if (clip?.writeText) {
        void clip.writeText(value).then(done, () => undefined);
      } else {
        done();
      }
    } catch {
      /* clipboard blocked — the value stays selectable for a manual copy */
    }
  }, [value]);

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <label
          htmlFor={id}
          className="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          {label}
        </label>
        {hint ? (
          <span className="text-[11px] text-muted-foreground">{hint}</span>
        ) : null}
      </div>
      <div className="flex items-stretch gap-2">
        {multiline ? (
          <pre
            id={id}
            className="max-h-28 flex-1 overflow-auto rounded-md border border-border bg-muted/50 px-3 py-2 font-mono text-[11px] leading-relaxed text-foreground"
          >
            {value}
          </pre>
        ) : (
          <output
            id={id}
            className="flex-1 overflow-x-auto whitespace-nowrap rounded-md border border-border bg-muted/50 px-3 py-2 font-mono text-xs text-foreground"
          >
            {value}
          </output>
        )}
        <button
          type="button"
          onClick={onCopy}
          aria-label={`Copy ${label}`}
          className="inline-flex h-auto shrink-0 items-center gap-1.5 self-stretch rounded-md border border-border px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {copied ? (
            <Check className="h-3.5 w-3.5 text-success" aria-hidden />
          ) : (
            <Copy className="h-3.5 w-3.5" aria-hidden />
          )}
          <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
    </div>
  );
}
