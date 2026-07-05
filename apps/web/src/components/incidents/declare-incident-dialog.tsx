"use client";

import { Siren } from "lucide-react";
import { useId, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useDeclareIncident } from "@/lib/api/incidents";
import { INCIDENT_SEVERITIES, type IncidentSeverity, type IncidentView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function declareErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to declare incidents.";
    if (error.status === 422)
      return "That project id doesn't look valid. Check it and try again.";
  }
  return "Couldn't declare the incident. Please try again.";
}

export interface DeclareIncidentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Prefilled project scope (the currently-viewed incident's project). */
  defaultProjectId?: string;
  onDeclared?: (incident: IncidentView) => void;
  client?: ForgeApiClient;
}

/**
 * The "Declare incident" form dialog — the screen's single ember primary action.
 * Title is required; severity defaults to medium; the project is prefilled from
 * the active incident's scope so declaring "in the same project" is one field.
 */
export function DeclareIncidentDialog({
  open,
  onOpenChange,
  defaultProjectId = "",
  onDeclared,
  client = apiClient,
}: DeclareIncidentDialogProps) {
  const titleId = useId();
  const severityId = useId();
  const projectId = useId();
  const descriptionId = useId();

  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState<IncidentSeverity>("medium");
  const [project, setProject] = useState(defaultProjectId);
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const declare = useDeclareIncident(client);

  // Reset the form on each open transition (fresh declaration) — a render-time
  // state adjustment rather than an effect, so the fields are clean on the
  // dialog's first painted frame.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setTitle("");
      setSeverity("medium");
      setProject(defaultProjectId);
      setDescription("");
      setError(null);
    }
  }

  const canSubmit =
    title.trim().length > 0 && project.trim().length > 0 && !declare.isPending;

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setError(null);
    declare.mutate(
      {
        project_id: project.trim(),
        title: title.trim(),
        severity,
        description: description.trim() || null,
      },
      {
        onSuccess: (incident) => {
          onDeclared?.(incident);
          onOpenChange(false);
        },
        onError: (err) => setError(declareErrorMessage(err)),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-display">
            <Siren aria-hidden className="h-5 w-5 text-primary" />
            Declare incident
          </DialogTitle>
          <DialogDescription>
            Opens a new incident at the <span className="font-mono">incident_created</span>{" "}
            state and starts the response timeline.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={titleId} className="text-sm font-medium">
              Title
            </label>
            <input
              id={titleId}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Checkout latency spike in eu-west"
              autoFocus
              required
              className={FIELD}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <label htmlFor={severityId} className="text-sm font-medium">
                Severity
              </label>
              <select
                id={severityId}
                value={severity}
                onChange={(e) => setSeverity(e.target.value as IncidentSeverity)}
                className={cn(FIELD, "capitalize")}
              >
                {INCIDENT_SEVERITIES.map((s) => (
                  <option key={s} value={s} className="capitalize">
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={projectId} className="text-sm font-medium">
                Project
              </label>
              <input
                id={projectId}
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder="project id"
                required
                className={cn(FIELD, "font-mono text-xs")}
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor={descriptionId} className="text-sm font-medium">
              What&apos;s happening?{" "}
              <span className="font-normal text-muted-foreground">(optional)</span>
            </label>
            <textarea
              id={descriptionId}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              placeholder="Symptoms, affected surface, anything responders should know."
              className={cn(FIELD, "resize-none")}
            />
          </div>

          {error ? (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              {declare.isPending ? "Declaring…" : "Declare incident"}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
