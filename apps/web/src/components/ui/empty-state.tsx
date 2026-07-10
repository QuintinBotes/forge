import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface EmptyStateProps {
  /** Decorative icon (rendered muted, `aria-hidden` is caller's responsibility). */
  icon?: ReactNode;
  /** Short, specific headline — e.g. "No specs yet". */
  title: string;
  /** One line explaining what lives here / why it's empty. */
  description?: ReactNode;
  /** The next action — typically the view's primary button. */
  action?: ReactNode;
  /** Secondary/tertiary action (ghost or link). */
  secondaryAction?: ReactNode;
  className?: string;
  /** Optional test hook. */
  "data-testid"?: string;
}

/**
 * The shared empty-state primitive. Every list/data view uses it so an absence
 * of data always **names the next action** instead of showing a blank. Purely
 * presentational and token-driven; reuse it across screens for consistency.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  className,
  "data-testid": testId,
}: EmptyStateProps) {
  return (
    <div
      data-testid={testId}
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border bg-card/40 px-6 py-12 text-center",
        className,
      )}
    >
      {icon ? (
        <div className="flex h-11 w-11 items-center justify-center rounded-full bg-muted text-muted-foreground [&_svg]:h-5 [&_svg]:w-5">
          {icon}
        </div>
      ) : null}
      <div className="flex flex-col gap-1">
        <p className="font-display text-sm font-semibold tracking-tight text-foreground">
          {title}
        </p>
        {description ? (
          <p className="mx-auto max-w-sm text-sm text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {action || secondaryAction ? (
        <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
          {action}
          {secondaryAction}
        </div>
      ) : null}
    </div>
  );
}
