"use client";

import { useId } from "react";

import { cn } from "@/lib/utils";

export interface SsoSwitchProps {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
  /**
   * `success` = the "SSO is live" master switch (green when on); `steel` = the
   * quiet secondary toggles (dark track when on). Ember is never used here — it
   * is reserved for the screen's single primary Save action.
   */
  tone?: "success" | "steel";
}

/**
 * A labelled, keyboard-operable switch (`role="switch"`, Space/Enter toggle via
 * the native button). Used both for the master enable control and the SAML
 * security options so the whole screen shares one interaction + focus model.
 */
export function SsoSwitch({
  checked,
  onCheckedChange,
  label,
  description,
  disabled = false,
  tone = "steel",
}: SsoSwitchProps) {
  const labelId = useId();
  const descId = useId();
  const onTrack =
    tone === "success" ? "bg-success" : "bg-foreground";

  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col">
        <span id={labelId} className="text-sm font-medium text-foreground">
          {label}
        </span>
        {description ? (
          <span id={descId} className="text-xs text-muted-foreground">
            {description}
          </span>
        ) : null}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={labelId}
        aria-describedby={description ? descId : undefined}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50",
          checked ? onTrack : "bg-muted-foreground/30",
        )}
      >
        <span
          className={cn(
            "inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          )}
        />
      </button>
    </div>
  );
}
