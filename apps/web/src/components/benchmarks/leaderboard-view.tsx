"use client";

import { BadgeCheck, ShieldQuestion, Trophy } from "lucide-react";
import { useMemo, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { usePublicBenchmarks, usePublicLeaderboard } from "@/lib/api/benchmarks";
import type {
  PublicBenchmark,
  PublicLeaderboard,
  PublicLeaderboardEntry,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

interface Selection {
  slug: string;
  version: string;
}

function suiteKey(suite: Pick<PublicBenchmark, "slug" | "version">): string {
  return `${suite.slug}@${suite.version}`;
}

function formatScore(score: number): string {
  return score.toFixed(3);
}

function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(0, 10);
}

export interface LeaderboardViewProps {
  client?: ForgeApiClient;
}

/**
 * The public benchmark leaderboard (F35): a suite picker over the ranked,
 * verified-first submissions to each published benchmark suite, read straight
 * from the unauthenticated `/public` router. Payload-free by construction —
 * every value rendered here is already present on the `Public*` response
 * shapes, so there is nothing sensitive to accidentally leak.
 */
export function LeaderboardView({ client = apiClient }: LeaderboardViewProps) {
  const [selected, setSelected] = useState<Selection | null>(null);

  const suitesQuery = usePublicBenchmarks(client);
  const suites = useMemo(() => suitesQuery.data ?? [], [suitesQuery.data]);

  // Effective selection derived during render: honour the explicit pick when
  // it survives the current suite list, else fall back to the first suite.
  const effective: Selection | null = useMemo(() => {
    if (
      selected &&
      suites.some(
        (s) => s.slug === selected.slug && s.version === selected.version,
      )
    ) {
      return selected;
    }
    const first = suites[0];
    return first ? { slug: first.slug, version: first.version } : null;
  }, [selected, suites]);

  const leaderboardQuery = usePublicLeaderboard(
    effective?.slug ?? null,
    effective?.version ?? null,
    client,
  );

  const activeSuite = effective
    ? (suites.find(
        (s) => s.slug === effective.slug && s.version === effective.version,
      ) ?? null)
    : null;

  const isSuitesLoading = suitesQuery.isLoading;
  const hasSuites = suites.length > 0;

  return (
    <div
      data-testid="leaderboard-view"
      role="region"
      aria-label="Benchmark leaderboard"
      className="flex h-full flex-col gap-4"
    >
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Trophy className="h-5 w-5 text-primary" aria-hidden />
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Leaderboard
          </h1>
          <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
            {suites.length} {suites.length === 1 ? "suite" : "suites"}
          </span>
        </div>
        <p className="max-w-md text-sm text-muted-foreground">
          Ranked, independently reproducible submissions to Forge&apos;s
          published benchmark suites.
        </p>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[16rem_1fr]">
        {/* Suite picker */}
        <div className="min-h-0 overflow-y-auto rounded-lg border border-border bg-card">
          {isSuitesLoading ? (
            <SuiteListSkeleton />
          ) : suitesQuery.isError ? (
            <ErrorState
              data-testid="suites-error"
              title="Suites unavailable"
              description="The public leaderboard couldn't be reached — check back shortly."
              onRetry={() => void suitesQuery.refetch()}
              className="h-full justify-center border-none bg-transparent"
            />
          ) : !hasSuites ? (
            <EmptyState
              data-testid="empty-suites"
              icon={<Trophy />}
              title="No benchmark suites yet"
              description="Published suites will appear here once available."
              className="h-full justify-center border-none bg-transparent"
            />
          ) : (
            <ul aria-label="Benchmark suites" className="flex flex-col gap-1 p-2">
              {suites.map((suite) => {
                const active =
                  effective?.slug === suite.slug &&
                  effective?.version === suite.version;
                return (
                  <li key={suiteKey(suite)}>
                    <button
                      type="button"
                      data-testid={`suite-${suite.slug}-${suite.version}`}
                      aria-current={active ? "true" : undefined}
                      onClick={() =>
                        setSelected({ slug: suite.slug, version: suite.version })
                      }
                      className={cn(
                        "flex w-full flex-col gap-0.5 rounded-md px-3 py-2 text-left text-sm transition-colors",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        active
                          ? "bg-accent text-accent-foreground"
                          : "text-foreground hover:bg-accent/60",
                      )}
                    >
                      <span className="truncate font-medium">{suite.title}</span>
                      <span className="truncate text-[11px] text-muted-foreground">
                        v{suite.version} · {suite.task_count} tasks
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Ranked leaderboard */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card">
          {!activeSuite ? (
            <EmptyState
              icon={<Trophy />}
              title={isSuitesLoading ? "Loading suites…" : "Select a suite"}
              description={
                isSuitesLoading
                  ? undefined
                  : "Pick a benchmark suite to see its ranked leaderboard."
              }
              className="h-full border-none bg-transparent"
            />
          ) : (
            <LeaderboardPanel suite={activeSuite} query={leaderboardQuery} />
          )}
        </div>
      </div>
    </div>
  );
}

// --- Leaderboard panel ------------------------------------------------------ //

function LeaderboardPanel({
  suite,
  query,
}: {
  suite: PublicBenchmark;
  query: UseQueryResult<PublicLeaderboard>;
}) {
  const entries = query.data?.entries ?? [];
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="truncate font-display text-base font-semibold text-foreground">
            {suite.title}
          </h2>
          <p className="truncate text-xs text-muted-foreground">
            {suite.primary_metric} · v{suite.version} · {suite.task_count} tasks
          </p>
        </div>
        {query.data ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            Updated {formatDate(query.data.generated_at)}
          </span>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {query.isLoading ? (
          <TableSkeleton />
        ) : query.isError ? (
          <ErrorState
            data-testid="leaderboard-error"
            title="Leaderboard unavailable"
            description="This suite's leaderboard couldn't be loaded — check back shortly."
            onRetry={() => void query.refetch()}
            className="h-full justify-center border-none bg-transparent"
          />
        ) : entries.length === 0 ? (
          <EmptyState
            data-testid="empty-leaderboard"
            icon={<Trophy />}
            title="No submissions yet"
            description="Once a submission is scored and published, it will be ranked here."
            className="h-full justify-center border-none bg-transparent"
          />
        ) : (
          <table
            data-testid="leaderboard-table"
            className="w-full min-w-[42rem] border-collapse text-left text-sm"
          >
            <thead>
              <tr className="border-b border-border bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="px-4 py-2 font-medium">
                  Rank
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Submitter
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Model
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Score
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <LeaderboardRow key={entry.submission_id} entry={entry} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function LeaderboardRow({ entry }: { entry: PublicLeaderboardEntry }) {
  return (
    <tr
      data-testid="leaderboard-row"
      className="border-b border-border/60 align-top last:border-b-0 hover:bg-accent/40"
    >
      <td className="px-4 py-3">
        <span
          className={cn(
            "inline-flex h-6 min-w-6 items-center justify-center rounded-full px-1.5 font-mono text-xs font-semibold",
            entry.rank === 1
              ? "bg-primary/15 text-primary"
              : "bg-muted text-muted-foreground",
          )}
        >
          #{entry.rank}
        </span>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-col gap-0.5">
          <span className="font-medium text-foreground">
            {entry.submitter_name}
          </span>
          {entry.submitter_org ? (
            <span className="text-[11px] text-muted-foreground">
              {entry.submitter_org}
            </span>
          ) : null}
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-foreground">{entry.model_label}</span>
          <span className="text-[11px] text-muted-foreground">
            {entry.agent_mode}
            {entry.forge_version ? ` · forge ${entry.forge_version}` : ""}
          </span>
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-col gap-1">
          <span className="font-mono text-sm font-semibold text-foreground">
            {formatScore(entry.composite_score)}
          </span>
          {entry.per_category.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {entry.per_category.slice(0, 3).map((cat) => (
                <span
                  key={cat.category}
                  className="rounded border border-border bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  {cat.category}: {cat.score.toFixed(2)}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </td>
      <td className="px-4 py-3 text-right">
        <VerifiedBadge verified={entry.verified} />
      </td>
    </tr>
  );
}

function VerifiedBadge({ verified }: { verified: boolean }) {
  if (verified) {
    return (
      <span
        data-testid="verified-badge"
        className="inline-flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-2 py-0.5 text-[11px] font-medium text-success"
      >
        <BadgeCheck className="h-3 w-3" aria-hidden />
        Verified
      </span>
    );
  }
  return (
    <span
      data-testid="unverified-badge"
      className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] font-medium text-warning"
    >
      <ShieldQuestion className="h-3 w-3" aria-hidden />
      Self-reported
    </span>
  );
}

// --- Loading skeletons ------------------------------------------------------ //

function SuiteListSkeleton() {
  return (
    <Loading
      data-testid="suites-skeleton"
      label="Loading benchmark suites…"
      className="flex flex-col gap-2 p-3"
    >
      {[0, 1, 2].map((i) => (
        <Skeleton key={i} className="h-12 rounded-md" />
      ))}
    </Loading>
  );
}

function TableSkeleton() {
  return (
    <Loading
      data-testid="leaderboard-skeleton"
      label="Loading leaderboard…"
      className="flex flex-col gap-2 p-4"
    >
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-10 rounded-md" />
      ))}
    </Loading>
  );
}
