"use client";

import type { ApprovalSummary } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  gateMeta,
  riskDotClass,
  riskBadgeClass,
  riskLabel,
} from "./approval-meta";
import { actorLabel, relativeTime } from "./format";

export interface ApprovalListProps {
  items: ApprovalSummary[];
  selectedId: string | null;
  onSelect: (item: ApprovalSummary) => void;
}

/**
 * The pending-approval queue (presentational).
 *
 * A risk-ranked list of gates; the selected row carries the ember rail. Row
 * navigation + shortcuts live in {@link ApprovalInbox} so `j/k/a/r/x/e` work
 * from anywhere on the screen (spec: keyboard-first, no mouse required).
 */
export function ApprovalList({ items, selectedId, onSelect }: ApprovalListProps) {
  return (
    <ul
      role="listbox"
      aria-label="Pending approvals"
      data-testid="approval-list"
      className="flex flex-col gap-1"
    >
      {items.map((item) => {
        const meta = gateMeta(item.gate_type);
        const Icon = meta.icon;
        const isSelected = item.id === selectedId;
        const risk = item.risk_level ?? "info";
        return (
          <li key={item.id}>
            <button
              type="button"
              role="option"
              aria-selected={isSelected}
              data-testid={`approval-row-${item.id}`}
              onClick={() => onSelect(item)}
              className={cn(
                "group relative flex w-full items-start gap-3 rounded-md border py-2.5 pl-4 pr-3 text-left transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                isSelected
                  ? "border-border bg-accent"
                  : "border-transparent hover:bg-accent/50",
              )}
            >
              {/* Risk rail — ember when selected, else risk-coloured. */}
              <span
                aria-hidden
                className={cn(
                  "absolute inset-y-1.5 left-1 w-1 rounded-full",
                  isSelected ? "bg-primary" : riskDotClass(risk),
                )}
              />
              <Icon
                aria-hidden
                className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground group-hover:text-foreground"
              />
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-foreground">
                    {item.title || meta.label}
                  </span>
                  {risk !== "info" ? (
                    <span
                      className={cn(
                        "shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                        riskBadgeClass(risk),
                      )}
                    >
                      {riskLabel(risk)}
                    </span>
                  ) : null}
                </span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="uppercase tracking-wide">{meta.label}</span>
                  <span aria-hidden>·</span>
                  <span className="truncate">{actorLabel(item.requested_actor)}</span>
                  <span aria-hidden>·</span>
                  <span className="whitespace-nowrap">
                    {relativeTime(item.requested_at)}
                  </span>
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
