"use client";

import { Workflow } from "lucide-react";
import { useId, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useCreateWorkflowDefinition } from "@/lib/api/workflow";
import type { WorkflowDefinitionDetail } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const NAME_PATTERN = /^[a-z][a-z0-9_]{1,62}$/;

export interface NewWorkflowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: (definition: WorkflowDefinitionDetail) => void;
  client?: ForgeApiClient;
}

function createErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You need admin access to create a workflow.";
    if (error.status === 409) return "A workflow with that name already exists.";
    if (error.status === 422)
      return "That name is invalid. Use lower_snake_case.";
  }
  return "Couldn't create the workflow. Please try again.";
}

/**
 * Create a new custom workflow — the empty-state first action. Seeds an initial
 * one-state draft server-side; the editor opens on it so the author can start
 * adding transitions immediately.
 */
export function NewWorkflowDialog({
  open,
  onOpenChange,
  onCreated,
  client = apiClient,
}: NewWorkflowDialogProps) {
  const nameId = useId();
  const titleId = useId();

  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useCreateWorkflowDefinition(client);

  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setName("");
      setTitle("");
      setError(null);
    }
  }

  const nameValid = NAME_PATTERN.test(name);
  const canSubmit =
    nameValid && title.trim().length > 0 && !create.isPending;

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setError(null);
    create.mutate(
      { name: name.trim(), title: title.trim() },
      {
        onSuccess: (definition) => {
          onCreated?.(definition);
          onOpenChange(false);
        },
        onError: (err) => setError(createErrorMessage(err)),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-display">
            <Workflow aria-hidden className="h-5 w-5 text-primary" />
            New workflow
          </DialogTitle>
          <DialogDescription>
            Creates a custom workflow with a starter draft. You can add states and
            transitions right after.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="text-sm font-medium">
              Name
            </label>
            <input
              id={nameId}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="release_review"
              autoFocus
              className={cn(FIELD, "font-mono text-xs")}
            />
            <p className="text-[11px] text-muted-foreground">
              Lowercase letters, digits and underscores (used as the DSL id).
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor={titleId} className="text-sm font-medium">
              Title
            </label>
            <input
              id={titleId}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Release review"
              className={FIELD}
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
              {create.isPending ? "Creating…" : "Create workflow"}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
