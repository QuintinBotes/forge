import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A single shimmering placeholder block. Use it to reserve the exact space a
 * value will occupy so async content swaps in with **no layout shift**. Honours
 * `prefers-reduced-motion` (the pulse stops). Decorative by default
 * (`aria-hidden`) — wrap groups in {@link Loading} to announce busy state.
 */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn(
        "animate-pulse rounded-md bg-muted/60 motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  );
}

export interface LoadingProps {
  /** The skeleton placeholders to render while loading. */
  children: ReactNode;
  /** Screen-reader announcement (visually hidden). */
  label?: string;
  className?: string;
  /** Optional test hook. */
  "data-testid"?: string;
}

/**
 * Announces an async region as busy for assistive tech and renders its
 * skeleton children. Pair the skeletons' dimensions with the loaded content so
 * there is no layout shift when data arrives.
 */
export function Loading({
  children,
  label = "Loading…",
  className,
  "data-testid": testId,
}: LoadingProps) {
  return (
    <div role="status" aria-busy="true" data-testid={testId} className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}
