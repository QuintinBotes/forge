"use client";

/**
 * The Proposals tab: the pending queue, newest first. Machines propose —
 * ingest, agents and the `world_propose` MCP tool — and people decide
 * here. Each card shows the proposed statement (payload keys rendered
 * defensively), who proposed it and why, and the shadow gate's decision
 * when present. Accepting writes the fact; a refusal from the API (a
 * viewer deciding, or a proposer deciding their own proposal) is shown
 * with the API's own `detail`.
 */

import { Inbox } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import type { ProposalOut } from "@/lib/api/types";

import {
  errorDetail,
  formatTimestamp,
  gateSummary,
  proposalPayloadEntries,
} from "./world-format";

export interface ProposalsPanelProps {
  proposals?: ProposalOut[];
  isLoading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Id of the proposal whose decision is in flight, if any. */
  decidingId?: string | null;
  /** The decision failure to show on its card (the API's `detail`). */
  decisionError?: { proposalId: string; error: unknown } | null;
  onDecide: (proposalId: string, accept: boolean, reason?: string) => void;
}

export function ProposalsPanel({
  proposals,
  isLoading = false,
  error,
  onRetry,
  decidingId = null,
  decisionError = null,
  onDecide,
}: ProposalsPanelProps) {
  const [reasons, setReasons] = useState<Record<string, string>>({});

  if (isLoading) {
    return (
      <Loading label="Loading proposals…">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </Loading>
    );
  }
  if (error) {
    return (
      <ErrorState
        title="Proposals didn't load"
        description={errorDetail(error)}
        onRetry={onRetry}
      />
    );
  }
  if (!proposals || proposals.length === 0) {
    return (
      <EmptyState
        icon={<Inbox aria-hidden />}
        title="No pending proposals"
        description="Proposed facts from ingest, agents and the world_propose MCP tool land here for a person to decide."
      />
    );
  }

  const reasonFor = (proposalId: string) => {
    const reason = (reasons[proposalId] ?? "").trim();
    return reason === "" ? undefined : reason;
  };

  return (
    <ul className="flex flex-col gap-3">
      {proposals.map((proposal) => {
        const entries = proposalPayloadEntries(proposal.payload);
        const gate = gateSummary(proposal.gate);
        const isDeciding = decidingId === proposal.id;
        const cardError =
          decisionError?.proposalId === proposal.id
            ? errorDetail(decisionError.error)
            : null;
        return (
          <li
            key={proposal.id}
            className="flex flex-col gap-3 rounded-lg border border-border bg-card px-4 py-3"
          >
            {entries.length > 0 ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                {entries.map((entry) => (
                  <div key={entry.key} className="contents">
                    <dt className="font-medium text-muted-foreground">
                      {entry.key}
                    </dt>
                    <dd className="text-foreground">{entry.value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-sm text-muted-foreground">
                No statement details in this proposal&apos;s payload.
              </p>
            )}

            <div className="flex flex-col gap-1 text-sm text-muted-foreground">
              <p>
                Proposed by {proposal.proposer_kind} · {proposal.proposer} ·{" "}
                {formatTimestamp(proposal.created_at)}
              </p>
              {proposal.rationale ? <p>{proposal.rationale}</p> : null}
              {gate ? <p>Gate: {gate}</p> : null}
            </div>

            <div className="flex flex-col gap-2">
              <label
                htmlFor={`proposal-reason-${proposal.id}`}
                className="text-sm font-medium text-foreground"
              >
                Reason (optional)
              </label>
              <input
                id={`proposal-reason-${proposal.id}`}
                type="text"
                value={reasons[proposal.id] ?? ""}
                onChange={(event) =>
                  setReasons((previous) => ({
                    ...previous,
                    [proposal.id]: event.target.value,
                  }))
                }
                className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            {cardError ? (
              <p role="alert" className="text-sm text-destructive">
                {cardError}
              </p>
            ) : null}

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                disabled={isDeciding}
                onClick={() =>
                  onDecide(proposal.id, true, reasonFor(proposal.id))
                }
              >
                Accept
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={isDeciding}
                onClick={() =>
                  onDecide(proposal.id, false, reasonFor(proposal.id))
                }
              >
                Reject
              </Button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
