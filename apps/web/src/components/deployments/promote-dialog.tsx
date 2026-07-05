"use client";

import { Rocket } from "lucide-react";
import { useId, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useRequestDeployment } from "@/lib/api/deployments";
import type { DeploymentRead, EnvironmentRead } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { sortEnvironmentsByRank } from "./deployment-meta";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export interface PromoteTarget {
  environment: string;
  commitSha: string;
}

export interface PromoteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  environments: EnvironmentRead[];
  /** Prefill (from a stage's "Promote →": next env + the stage's live commit). */
  initialTarget?: PromoteTarget | null;
  onPromoted?: (deployment: DeploymentRead) => void;
  client?: ForgeApiClient;
}

function promoteErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to promote in this workspace.";
    if (error.status === 422)
      return "That commit or environment looks invalid. Check them and retry.";
    if (error.status === 409) {
      const detail =
        error.body && typeof error.body === "object"
          ? (error.body as { detail?: unknown }).detail
          : undefined;
      if (detail && typeof detail === "object" && "blocking_reasons" in detail) {
        const reasons = (detail as { blocking_reasons?: unknown }).blocking_reasons;
        if (Array.isArray(reasons) && reasons.length > 0) {
          return `Gate blocked: ${reasons.join("; ")}`;
        }
        return "The gate is blocking this promotion.";
      }
      return "A deployment for this commit and environment is already in flight.";
    }
  }
  return "Couldn't start the promotion. Please try again.";
}

/**
 * The "Promote" dialog — the screen's single ember primary action. Requests a
 * new promotion of a commit to a target environment; the gate then evaluates it.
 * Prefilled from a stage's Promote (next env + that stage's live commit) so the
 * common path is one confirm.
 */
export function PromoteDialog({
  open,
  onOpenChange,
  projectId,
  environments,
  initialTarget,
  onPromoted,
  client = apiClient,
}: PromoteDialogProps) {
  const envId = useId();
  const commitId = useId();

  const ranked = sortEnvironmentsByRank(environments);
  const firstEnv = ranked[0]?.name ?? "";

  const [environment, setEnvironment] = useState(
    initialTarget?.environment ?? firstEnv,
  );
  const [commitSha, setCommitSha] = useState(initialTarget?.commitSha ?? "");
  const [error, setError] = useState<string | null>(null);

  const promote = useRequestDeployment(client);

  // Reset the form on each open transition (render-time state adjustment), so
  // the prefill from the triggering stage is present on the first painted frame.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setEnvironment(initialTarget?.environment ?? firstEnv);
      setCommitSha(initialTarget?.commitSha ?? "");
      setError(null);
    }
  }

  const canSubmit =
    environment.trim().length > 0 &&
    commitSha.trim().length > 0 &&
    !promote.isPending;

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setError(null);
    promote.mutate(
      {
        projectId,
        body: {
          environment: environment.trim(),
          commit_sha: commitSha.trim(),
          kind: "promotion",
        },
      },
      {
        onSuccess: (deployment) => {
          onPromoted?.(deployment);
          onOpenChange(false);
        },
        onError: (err) => setError(promoteErrorMessage(err)),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-display">
            <Rocket aria-hidden className="h-5 w-5 text-primary" />
            Promote a deployment
          </DialogTitle>
          <DialogDescription>
            Requests a promotion of a commit to a target environment. The gate
            re-evaluates it before it can proceed.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={envId} className="text-sm font-medium">
              Target environment
            </label>
            <select
              id={envId}
              value={environment}
              onChange={(e) => setEnvironment(e.target.value)}
              className={FIELD}
            >
              {ranked.map((env) => (
                <option key={env.id} value={env.name}>
                  {env.name}
                  {env.requires_approval ? " · requires approval" : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor={commitId} className="text-sm font-medium">
              Commit SHA
            </label>
            <input
              id={commitId}
              value={commitSha}
              onChange={(e) => setCommitSha(e.target.value)}
              placeholder="e.g. 9f3c1a2…"
              autoFocus
              required
              className={cn(FIELD, "font-mono text-xs")}
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
              <Rocket className="h-4 w-4" aria-hidden />
              {promote.isPending ? "Promoting…" : "Promote"}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
