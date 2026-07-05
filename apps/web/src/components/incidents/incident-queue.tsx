import type { IncidentView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { LifecycleBadge } from "./incident-badges";
import { relativeTime, severityMeta } from "./incident-meta";

export interface IncidentQueueProps {
  items: IncidentView[];
  selectedId: string | null;
  onSelect: (incident: IncidentView) => void;
}

/** The incident queue: severity-dotted rows, most urgent first. */
export function IncidentQueue({ items, selectedId, onSelect }: IncidentQueueProps) {
  return (
    <ul role="listbox" aria-label="Incidents" className="flex flex-col gap-1">
      {items.map((incident) => {
        const selected = incident.id === selectedId;
        const sev = severityMeta(incident.severity);
        return (
          <li key={incident.id} role="option" aria-selected={selected}>
            <button
              type="button"
              onClick={() => onSelect(incident)}
              className={cn(
                "flex w-full flex-col gap-1.5 rounded-md border px-3 py-2.5 text-left transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selected
                  ? "border-primary/40 bg-accent"
                  : "border-transparent hover:bg-accent/60",
              )}
            >
              <div className="flex items-center gap-2">
                <span
                  aria-hidden
                  className={cn("h-2 w-2 shrink-0 rounded-full", sev.dotClass)}
                />
                <span className="font-mono text-[11px] text-muted-foreground">
                  {incident.key}
                </span>
                <span className="ml-auto text-[11px] text-muted-foreground">
                  {relativeTime(incident.created_at)}
                </span>
              </div>
              <p className="truncate text-sm font-medium text-foreground">
                {incident.title}
              </p>
              <div className="flex items-center gap-1.5">
                <LifecycleBadge state={incident.lifecycle_state} />
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
