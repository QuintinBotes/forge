"use client";

/**
 * The Knowledge graph view (`/world`): search the bitemporal world model,
 * travel on both of its clocks, explore an entity's neighbourhood as a
 * graph, read its facts with their evidence, and — in the other two tabs —
 * inspect conflicts and decide proposed facts. Machines propose; people
 * decide.
 */

import { Network } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import type { WorldTimeQuery } from "@/lib/api/types";
import {
  useDecideWorldProposal,
  useWorldConflicts,
  useWorldEntities,
  useWorldEntityChanges,
  useWorldEntityProfile,
  useWorldNeighbourEntities,
  useWorldProposals,
} from "@/lib/api/world";
import { cn } from "@/lib/utils";

import { ConflictsPanel } from "./conflicts-panel";
import { EntityProfile } from "./entity-profile";
import { ProposalsPanel } from "./proposals-panel";
import { WorldGraph, type GraphEdge } from "./world-graph";
import { errorDetail, shortId } from "./world-format";

/** How many neighbours the graph resolves and draws (the rest are "+N more"). */
export const NEIGHBOUR_CAP = 24;

type WorldTab = "explore" | "conflicts" | "proposals";

const TABS: { id: WorldTab; label: string }[] = [
  { id: "explore", label: "Explore" },
  { id: "conflicts", label: "Conflicts" },
  { id: "proposals", label: "Proposals" },
];

/** A `datetime-local` value as an ISO string (empty/invalid means "now"). */
function toIso(value: string): string | undefined {
  if (value === "") {
    return undefined;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export interface WorldViewProps {
  client?: ForgeApiClient;
}

export function WorldView({ client = apiClient }: WorldViewProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const entityParam = searchParams?.get("entity") ?? null;

  const [tab, setTab] = useState<WorldTab>("explore");
  const [searchText, setSearchText] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [atInput, setAtInput] = useState("");
  const [asOfInput, setAsOfInput] = useState("");
  // The focused entity: an explicit in-view pick wins, else the linkable
  // `?entity=` param. `focusEntity` keeps the two in step via `replace`.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const focusedId = selectedId ?? entityParam;
  const [decisionError, setDecisionError] = useState<{
    proposalId: string;
    error: unknown;
  } | null>(null);

  // Debounce the search box so typing does not fire a query per keystroke.
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedSearch(searchText), 300);
    return () => clearTimeout(handle);
  }, [searchText]);

  const at = toIso(atInput);
  const asOf = toIso(asOfInput);
  const time = useMemo<WorldTimeQuery>(() => ({ at, as_of: asOf }), [at, asOf]);

  const focusEntity = useCallback(
    (entityId: string) => {
      setSelectedId(entityId);
      setTab("explore");
      router.replace(`/world?entity=${encodeURIComponent(entityId)}`, {
        scroll: false,
      });
    },
    [router],
  );

  const entitiesQuery = useWorldEntities(
    {
      q: debouncedSearch.trim() === "" ? undefined : debouncedSearch.trim(),
      at,
      as_of: asOf,
      limit: 50,
    },
    client,
  );
  const profileQuery = useWorldEntityProfile(focusedId, time, client);
  const changesQuery = useWorldEntityChanges(focusedId, undefined, client);
  const conflictsQuery = useWorldConflicts(at, client);
  const proposalsQuery = useWorldProposals({ status: "proposed" }, client);
  const decideProposal = useDecideWorldProposal(client);

  const profile = profileQuery.data;

  // Neighbour ids in first-appearance order (outgoing facts, then incoming),
  // capped so a hub entity cannot fan out into hundreds of profile queries.
  const allNeighbourIds = useMemo(() => {
    if (!profile) {
      return [];
    }
    const seen = new Set<string>([profile.entity.id, profile.resolved_id]);
    const ids: string[] = [];
    const add = (id: string | null) => {
      if (id && !seen.has(id)) {
        seen.add(id);
        ids.push(id);
      }
    };
    for (const fact of profile.facts) {
      add(fact.object_id);
    }
    for (const fact of profile.object_of) {
      add(fact.subject_id);
    }
    return ids;
  }, [profile]);
  const neighbourIds = useMemo(
    () => allNeighbourIds.slice(0, NEIGHBOUR_CAP),
    [allNeighbourIds],
  );
  const hiddenNeighbours = allNeighbourIds.length - neighbourIds.length;
  const neighbours = useWorldNeighbourEntities(neighbourIds, time, client);

  const edges = useMemo<GraphEdge[]>(() => {
    if (!profile) {
      return [];
    }
    const shown = new Set(neighbourIds);
    const drawn: GraphEdge[] = [];
    for (const fact of profile.facts) {
      if (fact.object_id && shown.has(fact.object_id)) {
        drawn.push({
          id: `${fact.id}-outgoing`,
          neighbourId: fact.object_id,
          direction: "outgoing",
          label: fact.phrase || fact.predicate_key || "—",
          predicateKey: fact.predicate_key,
        });
      }
    }
    for (const fact of profile.object_of) {
      if (shown.has(fact.subject_id)) {
        drawn.push({
          id: `${fact.id}-incoming`,
          neighbourId: fact.subject_id,
          direction: "incoming",
          label: fact.phrase || fact.predicate_key || "—",
          predicateKey: fact.predicate_key,
        });
      }
    }
    return drawn;
  }, [profile, neighbourIds]);

  const resolveLabel = useCallback(
    (entityId: string): string => {
      if (profile && entityId === profile.entity.id) {
        return profile.entity.label;
      }
      const neighbour = neighbours.entities.get(entityId);
      if (neighbour) {
        return neighbour.label;
      }
      const searched = entitiesQuery.data?.items.find(
        (entity) => entity.id === entityId,
      );
      return searched?.label ?? shortId(entityId);
    },
    [profile, neighbours.entities, entitiesQuery.data],
  );

  const onDecide = useCallback(
    (proposalId: string, accept: boolean, reason?: string) => {
      setDecisionError(null);
      decideProposal.mutate(
        { proposalId, accept, reason },
        {
          onError: (error) => setDecisionError({ proposalId, error }),
        },
      );
    },
    [decideProposal],
  );

  const clearTime = useCallback(() => {
    setAtInput("");
    setAsOfInput("");
  }, []);

  const entities = entitiesQuery.data?.items ?? [];
  const workspaceEmpty =
    entitiesQuery.isSuccess &&
    entities.length === 0 &&
    debouncedSearch.trim() === "" &&
    !focusedId;

  let exploreMain: ReactNode;
  if (workspaceEmpty) {
    exploreMain = (
      <EmptyState
        icon={<Network aria-hidden />}
        title="No entities yet"
        description="Facts arrive through ingest, agents and the world_propose MCP tool. Proposed facts land in the Proposals tab for a person to decide."
      />
    );
  } else if (!focusedId) {
    exploreMain = (
      <div className="rounded-lg border border-dashed border-border bg-card/40 px-6 py-12 text-center">
        <p className="text-sm text-muted-foreground">
          Select an entity to explore its neighbourhood.
        </p>
      </div>
    );
  } else if (profileQuery.isLoading) {
    exploreMain = (
      <Loading label="Loading entity…">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      </Loading>
    );
  } else if (profileQuery.isError) {
    exploreMain = (
      <ErrorState
        title="Entity didn't load"
        description={errorDetail(profileQuery.error)}
        onRetry={() => void profileQuery.refetch()}
      />
    );
  } else if (profile) {
    exploreMain = (
      <div className="flex min-w-0 flex-col gap-6">
        <section aria-label="Neighbourhood" className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-foreground">
            Neighbourhood
          </h2>
          <WorldGraph
            focal={{ id: profile.entity.id, label: profile.entity.label }}
            neighbours={neighbourIds.map((id) => ({
              id,
              label: resolveLabel(id),
            }))}
            edges={edges}
            hiddenNeighbours={hiddenNeighbours}
            onFocusEntity={focusEntity}
          />
        </section>
        <EntityProfile
          profile={profile}
          resolveLabel={resolveLabel}
          onFocusEntity={focusEntity}
          changes={changesQuery.data?.items}
          changesIsLoading={changesQuery.isLoading}
          changesError={changesQuery.isError ? changesQuery.error : undefined}
          onRetryChanges={() => void changesQuery.refetch()}
        />
      </div>
    );
  } else {
    exploreMain = null;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">
          Knowledge graph
        </h1>
        <p className="text-sm text-muted-foreground">
          Forge&apos;s world model: entities, facts and their evidence, on two
          clocks. Machines propose; people decide.
        </p>
      </div>

      <section
        aria-label="Time travel"
        className="flex flex-col gap-2 rounded-lg border border-border bg-card px-4 py-3"
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label
              htmlFor="world-at"
              className="text-sm font-medium text-foreground"
            >
              World time (at)
            </label>
            <input
              id="world-at"
              type="datetime-local"
              value={atInput}
              onChange={(event) => setAtInput(event.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label
              htmlFor="world-as-of"
              className="text-sm font-medium text-foreground"
            >
              Known as of (as_of)
            </label>
            <input
              id="world-as-of"
              type="datetime-local"
              value={asOfInput}
              onChange={(event) => setAsOfInput(event.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <Button type="button" variant="outline" onClick={clearTime}>
            Now
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">
          World time is when something was true; known-as-of is what forge had
          recorded by then. Leave both empty for now.
        </p>
      </section>

      <div
        role="tablist"
        aria-label="Knowledge graph views"
        className="flex gap-1 border-b border-border"
      >
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`world-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls={`world-panel-${item.id}`}
            onClick={() => setTab(item.id)}
            className={cn(
              "border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              tab === item.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "explore" ? (
        <div
          role="tabpanel"
          id="world-panel-explore"
          aria-labelledby="world-tab-explore"
          className="grid items-start gap-4 lg:grid-cols-[280px_minmax(0,1fr)]"
        >
          <div className="flex flex-col gap-2">
            <label
              htmlFor="world-search"
              className="text-sm font-medium text-foreground"
            >
              Search entities
            </label>
            <input
              id="world-search"
              type="search"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Search entities…"
              className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            {entitiesQuery.isLoading ? (
              <Loading label="Searching entities…">
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-9 w-full" />
                  <Skeleton className="h-9 w-full" />
                  <Skeleton className="h-9 w-full" />
                </div>
              </Loading>
            ) : entitiesQuery.isError ? (
              <ErrorState
                title="Search didn't load"
                description={errorDetail(entitiesQuery.error)}
                onRetry={() => void entitiesQuery.refetch()}
              />
            ) : entities.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {debouncedSearch.trim() === ""
                  ? "No entities yet."
                  : "No entities match your search."}
              </p>
            ) : (
              <ul className="flex flex-col gap-1">
                {entities.map((entity) => (
                  <li key={entity.id}>
                    <button
                      type="button"
                      onClick={() => focusEntity(entity.id)}
                      aria-pressed={focusedId === entity.id}
                      className={cn(
                        "flex w-full flex-col rounded-md px-3 py-2 text-left text-sm transition-colors",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        focusedId === entity.id
                          ? "bg-accent text-accent-foreground"
                          : "text-foreground hover:bg-accent/60",
                      )}
                    >
                      <span className="font-medium">{entity.label}</span>
                      {entity.type_key ? (
                        <span className="text-xs text-muted-foreground">
                          {entity.type_key}
                        </span>
                      ) : null}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="min-w-0">{exploreMain}</div>
        </div>
      ) : null}

      {tab === "conflicts" ? (
        <div
          role="tabpanel"
          id="world-panel-conflicts"
          aria-labelledby="world-tab-conflicts"
        >
          <ConflictsPanel
            conflicts={conflictsQuery.data?.items}
            isLoading={conflictsQuery.isLoading}
            error={conflictsQuery.isError ? conflictsQuery.error : undefined}
            onRetry={() => void conflictsQuery.refetch()}
            resolveLabel={resolveLabel}
            onFocusEntity={focusEntity}
          />
        </div>
      ) : null}

      {tab === "proposals" ? (
        <div
          role="tabpanel"
          id="world-panel-proposals"
          aria-labelledby="world-tab-proposals"
        >
          <ProposalsPanel
            proposals={proposalsQuery.data?.items}
            isLoading={proposalsQuery.isLoading}
            error={proposalsQuery.isError ? proposalsQuery.error : undefined}
            onRetry={() => void proposalsQuery.refetch()}
            decidingId={
              decideProposal.isPending
                ? (decideProposal.variables?.proposalId ?? null)
                : null
            }
            decisionError={decisionError}
            onDecide={onDecide}
          />
        </div>
      ) : null}
    </div>
  );
}
