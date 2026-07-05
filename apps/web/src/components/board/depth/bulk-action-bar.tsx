"use client";

import { UserPlus, X } from "lucide-react";

import type { TaskStatus } from "@/lib/api/types";
import { STATUS_COLUMNS, STATUS_LABELS } from "@/lib/board/status";

export interface BulkActionBarProps {
  count: number;
  onSetStatus: (status: TaskStatus) => void;
  onAssignToMe: () => void;
  /** False when the viewer is unknown (disables "Assign to me"). */
  canAssign: boolean;
  onClear: () => void;
  pending?: boolean;
}

/**
 * Contextual bulk-action bar. Appears only while cards are selected; lets the
 * viewer restatus or reassign the whole selection in one call. Kept in the
 * steel/secondary register so the board's single ember action (New task) stays
 * precious.
 */
export function BulkActionBar({
  count,
  onSetStatus,
  onAssignToMe,
  canAssign,
  onClear,
  pending = false,
}: BulkActionBarProps) {
  if (count === 0) {
    return null;
  }

  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className="flex flex-wrap items-center gap-3 rounded-lg border border-primary/30 bg-card px-3 py-2 shadow-sm"
    >
      <span className="text-sm font-medium">
        <span className="font-mono">{count}</span> selected
      </span>

      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        Set status
        <select
          aria-label="Set status for selected"
          defaultValue=""
          disabled={pending}
          onChange={(event) => {
            const value = event.target.value;
            if (value) {
              onSetStatus(value as TaskStatus);
              event.target.value = "";
            }
          }}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
        >
          <option value="" disabled>
            Choose…
          </option>
          {STATUS_COLUMNS.map((status) => (
            <option key={status} value={status}>
              {STATUS_LABELS[status]}
            </option>
          ))}
        </select>
      </label>

      <button
        type="button"
        onClick={onAssignToMe}
        disabled={!canAssign || pending}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 text-xs font-medium hover:bg-accent hover:text-accent-foreground disabled:cursor-not-allowed disabled:opacity-50"
      >
        <UserPlus className="h-3.5 w-3.5" />
        Assign to me
      </button>

      <button
        type="button"
        onClick={onClear}
        className="ml-auto inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <X className="h-3.5 w-3.5" />
        Clear
      </button>
    </div>
  );
}
