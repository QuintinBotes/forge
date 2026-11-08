"use client";

/**
 * The knowledge base page (`/knowledge`): what forge has indexed — each
 * source's freshness and how much of it is embedded — and a search over it
 * that states which retrieval legs ran (ADR-0018) and opens whole documents
 * as plain text (ADR-0014: indexed text is untrusted, so it is never
 * rendered as HTML or markdown).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Library, RefreshCw } from "lucide-react";

import { formatTimestamp, errorDetail } from "@/components/world/world-format";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useCurrentUser } from "@/lib/api/hooks";
import type { KnowledgeSourceSummary } from "@/lib/api/types";

import { KnowledgeSearch } from "./knowledge-search";

const SOURCES_KEY = ["knowledge", "sources"] as const;

export interface KnowledgeViewProps {
  client?: ForgeApiClient;
}

export function KnowledgeView({ client = apiClient }: KnowledgeViewProps) {
  const sources = useQuery({
    queryKey: SOURCES_KEY,
    queryFn: () => client.listKnowledgeSources(),
  });

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">
          Knowledge base
        </h1>
        <p className="text-sm text-muted-foreground">
          What forge has indexed for retrieval, how fresh it is, and a search
          that says which retrieval legs ran.
        </p>
      </div>

      <section aria-labelledby="knowledge-sources" className="flex flex-col gap-2">
        <h2
          id="knowledge-sources"
          className="text-sm font-semibold text-foreground"
        >
          Sources
        </h2>
        {sources.isLoading ? (
          <Loading label="Loading sources…">
            <div className="flex flex-col gap-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          </Loading>
        ) : sources.isError ? (
          <ErrorState
            title="Sources didn't load"
            description={errorDetail(sources.error)}
            onRetry={() => void sources.refetch()}
          />
        ) : !sources.data || sources.data.length === 0 ? (
          <EmptyState
            icon={<Library aria-hidden />}
            title="No knowledge sources yet"
            description="Connect a repository, tracker or MCP server and sync it; its chunks appear here."
          />
        ) : (
          <SourcesTable sources={sources.data} client={client} />
        )}
      </section>

      <KnowledgeSearch client={client} sources={sources.data} />
    </div>
  );
}

function SourcesTable({
  sources,
  client,
}: {
  sources: KnowledgeSourceSummary[];
  client: ForgeApiClient;
}) {
  const isAdmin = useCurrentUser(client).data?.role === "admin";

  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-border text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">Source</th>
            <th scope="col" className="px-3 py-2 font-medium">Last synced</th>
            <th scope="col" className="px-3 py-2 font-medium">Newest chunk</th>
            <th scope="col" className="px-3 py-2 font-medium">Embedded</th>
            {isAdmin ? (
              <th scope="col" className="px-3 py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {sources.map((source) => (
            <SourceRow key={source.id} source={source} client={client} isAdmin={isAdmin} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One source; its own mutation, so a sync on one row never touches another. */
function SourceRow({
  source,
  client,
  isAdmin,
}: {
  source: KnowledgeSourceSummary;
  client: ForgeApiClient;
  isAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const sync = useMutation({
    mutationFn: () => client.syncKnowledgeSource(source.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
  const pct =
    source.chunk_count === 0
      ? 0
      : Math.round((source.embedded_chunk_count / source.chunk_count) * 100);

  return (
    <tr className="border-b border-border last:border-0 align-top">
      <th scope="row" className="px-3 py-2 font-normal">
        <div className="font-medium text-foreground">{source.name}</div>
        <div className="break-all text-xs text-muted-foreground">
          {source.kind}
          {source.tracker ? ` · ${source.tracker} tracker` : ""} · {source.uri}
        </div>
        <div className="text-xs text-muted-foreground">Sync mode: {source.sync_mode}</div>
      </th>
      <td className="px-3 py-2">
        <div className="whitespace-nowrap text-foreground">
          {source.last_synced_at ? formatTimestamp(source.last_synced_at) : "Never"}
        </div>
        {source.stale ? (
          <span className="mt-1 inline-block rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
            Stale
          </span>
        ) : null}
        <div className="text-xs text-muted-foreground">
          {source.freshness_sla_minutes === null
            ? "No freshness SLA"
            : `SLA ${source.freshness_sla_minutes} min`}
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-foreground">
        {source.newest_chunk_at ? formatTimestamp(source.newest_chunk_at) : "—"}
      </td>
      <td className="min-w-40 px-3 py-2">
        <div
          role="meter"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Embedding coverage for ${source.name}`}
          className="h-2 overflow-hidden rounded-full bg-muted"
        >
          <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {source.embedded_chunk_count} of {source.chunk_count} embedded
        </div>
      </td>
      {isAdmin ? (
        <td className="px-3 py-2">
          {source.syncable ? (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={sync.isPending}
                aria-busy={sync.isPending}
                onClick={() => sync.mutate()}
              >
                <RefreshCw aria-hidden />
                {sync.isPending ? "Syncing…" : "Sync now"}{" "}
                <span className="sr-only">{source.name}</span>
              </Button>
              <div aria-live="polite" className="mt-1 text-xs">
                {sync.isError ? (
                  <span role="alert" className="text-danger">
                    {errorDetail(sync.error)}
                  </span>
                ) : sync.isSuccess ? (
                  <span className="text-muted-foreground">Sync requested</span>
                ) : null}
              </div>
            </>
          ) : null}
        </td>
      ) : null}
    </tr>
  );
}
