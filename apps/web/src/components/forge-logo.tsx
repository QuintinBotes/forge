import type { SVGProps } from "react";

import { cn } from "@/lib/utils";

/**
 * The Forge mark: a geometric monogram "F" that reads as a struck anvil —
 * three heavy ember strokes standing on a steel plinth, with a bright spark
 * flying off the top-right. Colours are driven by design tokens so the mark
 * re-themes automatically in light and dark:
 *   - the F uses the ember `--primary`
 *   - the anvil base uses neutral `--muted-foreground` steel
 *   - the spark uses the bright `--spark` highlight
 *
 * Purely presentational (no hooks) so it renders in any server component.
 */
export function ForgeMark({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden
      className={cn("h-6 w-6", className)}
      {...props}
    >
      <path d="M15 45 H43 L47 53 H11 Z" fill="hsl(var(--muted-foreground))" />
      <g fill="hsl(var(--primary))">
        <rect x="17" y="13" width="10" height="32" rx="1.5" />
        <rect x="17" y="13" width="27" height="10" rx="1.5" />
        <rect x="17" y="27" width="19" height="9" rx="1.5" />
      </g>
      <path
        d="M48 7 L50.2 10.8 L54 13 L50.2 15.2 L48 19 L45.8 15.2 L42 13 L45.8 10.8 Z"
        fill="hsl(var(--spark))"
      />
    </svg>
  );
}
