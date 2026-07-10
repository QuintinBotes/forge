import { AlertTriangle, RotateCcw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ErrorStateProps {
  /** What failed, in the user's words. Avoid "Something went wrong". */
  title?: string;
  /** How to recover / what it means. Never a raw stack trace. */
  description?: ReactNode;
  /** When provided, renders a Retry button wired to this handler. */
  onRetry?: () => void;
  /** Override the retry button label. */
  retryLabel?: string;
  /** Extra action (e.g. "Contact support") rendered beside Retry. */
  action?: ReactNode;
  className?: string;
  /** Optional test hook. */
  "data-testid"?: string;
}

/**
 * The shared error-state primitive. Explains what went wrong, how to fix it,
 * and offers a retry — never a bare "something went wrong" or a raw stack.
 * Announced to assistive tech via `role="alert"`. Uses the semantic `danger`
 * token for the icon (kept separate from the ember brand accent).
 */
export function ErrorState({
  title = "This didn’t load",
  description = "The service is unreachable right now. Check your connection and try again.",
  onRetry,
  retryLabel = "Try again",
  action,
  className,
  "data-testid": testId,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border bg-card/40 px-6 py-12 text-center",
        className,
      )}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
        <AlertTriangle aria-hidden className="h-5 w-5" />
      </div>
      <div className="flex flex-col gap-1">
        <p className="font-display text-sm font-semibold tracking-tight text-foreground">
          {title}
        </p>
        {description ? (
          <p className="mx-auto max-w-sm text-sm text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {onRetry || action ? (
        <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
          {onRetry ? (
            <Button variant="outline" size="sm" onClick={onRetry}>
              <RotateCcw aria-hidden className="h-4 w-4" />
              {retryLabel}
            </Button>
          ) : null}
          {action}
        </div>
      ) : null}
    </div>
  );
}
