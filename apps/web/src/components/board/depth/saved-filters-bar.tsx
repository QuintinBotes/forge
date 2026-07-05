"use client";

import { Bookmark, Check, X } from "lucide-react";
import { useState, type FormEvent } from "react";

import type { BoardView } from "@/lib/board/filters";
import { cn } from "@/lib/utils";

export interface SavedFiltersBarProps {
  views: BoardView[];
  activeId: string;
  /** Ids of user-saved (removable) views. */
  removableIds: ReadonlySet<string>;
  onSelect: (view: BoardView) => void;
  onDelete: (id: string) => void;
  onSaveCurrent: (label: string) => void;
  /** Whether the current filter/query is worth saving as a view. */
  canSave: boolean;
}

/**
 * The saved-filters bar: preset views plus the viewer's own saved views as
 * selectable chips, and an inline "Save view" affordance that snapshots the
 * active filter + search into a new named view.
 */
export function SavedFiltersBar({
  views,
  activeId,
  removableIds,
  onSelect,
  onDelete,
  onSaveCurrent,
  canSave,
}: SavedFiltersBarProps) {
  const [saving, setSaving] = useState(false);
  const [label, setLabel] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = label.trim();
    if (!trimmed) {
      return;
    }
    onSaveCurrent(trimmed);
    setLabel("");
    setSaving(false);
  };

  return (
    <div
      role="toolbar"
      aria-label="Saved filters"
      className="flex items-center gap-2 overflow-x-auto pb-1"
    >
      {views.map((view) => {
        const active = view.id === activeId;
        const removable = removableIds.has(view.id);
        return (
          <span
            key={view.id}
            className={cn(
              "group inline-flex shrink-0 items-center rounded-full border text-xs transition-colors",
              active
                ? "border-primary bg-primary/10 text-foreground"
                : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground",
            )}
          >
            <button
              type="button"
              aria-pressed={active}
              onClick={() => onSelect(view)}
              className="rounded-full px-3 py-1 font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {view.label}
            </button>
            {removable ? (
              <button
                type="button"
                aria-label={`Delete ${view.label} view`}
                onClick={() => onDelete(view.id)}
                className="mr-1 rounded-full p-0.5 text-muted-foreground hover:text-danger"
              >
                <X className="h-3 w-3" />
              </button>
            ) : null}
          </span>
        );
      })}

      <div className="ml-auto shrink-0 pl-2">
        {saving ? (
          <form onSubmit={submit} className="flex items-center gap-1">
            <input
              autoFocus
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="View name"
              aria-label="View name"
              className="h-7 w-32 rounded-md border border-input bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <button
              type="submit"
              disabled={!label.trim()}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-xs font-medium hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
            >
              <Check className="h-3 w-3" />
              Save
            </button>
          </form>
        ) : (
          <button
            type="button"
            onClick={() => setSaving(true)}
            disabled={!canSave}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-md border border-dashed border-border px-2 text-xs font-medium text-muted-foreground",
              canSave
                ? "hover:border-primary/40 hover:text-foreground"
                : "cursor-not-allowed opacity-50",
            )}
          >
            <Bookmark className="h-3 w-3" />
            Save view
          </button>
        )}
      </div>
    </div>
  );
}
