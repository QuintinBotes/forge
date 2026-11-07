"use client";

/**
 * TanStack Query hooks for the bitemporal world model (the `/world` router).
 *
 * Every read is keyed by the two time-travel clocks it was taken on — `at`
 * (world time) and `as_of` (system time) — so travelling in time is just a
 * new query key, and "Now" falls back to the cached present. Neighbour
 * labels are not part of a profile, so the graph resolves them with one
 * profile query per neighbour via `useQueries`, sharing the profile cache
 * with the focal entity: clicking a neighbour refocuses onto data that is
 * already warm.
 *
 * Deciding a proposal is the one write. On success it invalidates the
 * proposal queue, every cached profile and the entity search, because an
 * accepted proposal writes a fact (and may create entities) that all three
 * surfaces show.
 */

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  ChangesOut,
  ConflictsOut,
  EntityListOut,
  EntityOut,
  EntityProfileOut,
  ProposalDecisionOut,
  ProposalListOut,
  WorldChangeQuery,
  WorldEntityQuery,
  WorldProposalQuery,
  WorldTimeQuery,
} from "./types";

export const worldKeys = {
  all: () => ["world"] as const,
  entityLists: () => ["world", "entities"] as const,
  entityList: (query?: WorldEntityQuery) =>
    ["world", "entities", query ?? {}] as const,
  profiles: () => ["world", "profile"] as const,
  profile: (entityId: string, time?: WorldTimeQuery) =>
    ["world", "profile", entityId, time ?? {}] as const,
  changes: (entityId: string, query?: WorldChangeQuery) =>
    ["world", "changes", entityId, query ?? {}] as const,
  conflicts: (at?: string) => ["world", "conflicts", { at }] as const,
  proposalLists: () => ["world", "proposals"] as const,
  proposalList: (query?: WorldProposalQuery) =>
    ["world", "proposals", query ?? {}] as const,
} as const;

/** Entity search (name/label, optionally typed) on both clocks. */
export function useWorldEntities(
  query?: WorldEntityQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<EntityListOut> {
  return useQuery({
    queryKey: worldKeys.entityList(query),
    queryFn: () => client.listWorldEntities(query),
  });
}

/** One entity's profile on both clocks; disabled until an entity is focused. */
export function useWorldEntityProfile(
  entityId: string | null,
  time?: WorldTimeQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<EntityProfileOut> {
  return useQuery({
    queryKey: worldKeys.profile(entityId ?? "", time),
    queryFn: () => client.getWorldEntity(entityId as string, time),
    enabled: Boolean(entityId),
  });
}

export interface WorldNeighbourEntities {
  /** Resolved neighbour entities by id (absent while loading or on error). */
  entities: Map<string, EntityOut>;
  /** True while any neighbour profile is still loading. */
  isLoading: boolean;
}

/**
 * Resolve neighbour entities (for graph labels) with one profile query per
 * id. Callers cap `entityIds` before calling; each query shares the
 * `worldKeys.profile` cache, so refocusing on a neighbour is instant.
 */
export function useWorldNeighbourEntities(
  entityIds: string[],
  time?: WorldTimeQuery,
  client: ForgeApiClient = apiClient,
): WorldNeighbourEntities {
  const results = useQueries({
    queries: entityIds.map((entityId) => ({
      queryKey: worldKeys.profile(entityId, time),
      queryFn: () => client.getWorldEntity(entityId, time),
    })),
  });
  const entities = new Map<string, EntityOut>();
  results.forEach((result, index) => {
    if (result.data) {
      entities.set(entityIds[index], result.data.entity);
    }
  });
  return {
    entities,
    isLoading: results.some((result) => result.isLoading),
  };
}

/** An entity's record-axis change timeline (asserted / invalidated). */
export function useWorldEntityChanges(
  entityId: string | null,
  query?: WorldChangeQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ChangesOut> {
  return useQuery({
    queryKey: worldKeys.changes(entityId ?? "", query),
    queryFn: () => client.getWorldEntityChanges(entityId as string, query),
    enabled: Boolean(entityId),
  });
}

/** Conflicts at a world time (the endpoint takes `at` only, never `as_of`). */
export function useWorldConflicts(
  at?: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ConflictsOut> {
  return useQuery({
    queryKey: worldKeys.conflicts(at),
    queryFn: () => client.listWorldConflicts({ at }),
  });
}

/** The proposal review queue, newest first, optionally filtered by status. */
export function useWorldProposals(
  query?: WorldProposalQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ProposalListOut> {
  return useQuery({
    queryKey: worldKeys.proposalList(query),
    queryFn: () => client.listWorldProposals(query),
  });
}

export interface DecideWorldProposalVariables {
  proposalId: string;
  accept: boolean;
  reason?: string;
}

/**
 * Accept or reject a proposal. On success the queue, all profiles and the
 * entity search are invalidated: an accepted proposal writes a fact (and
 * may create entities) that each of those surfaces shows.
 */
export function useDecideWorldProposal(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ProposalDecisionOut, Error, DecideWorldProposalVariables> {
  const queryClient = useQueryClient();
  return useMutation<ProposalDecisionOut, Error, DecideWorldProposalVariables>({
    mutationFn: ({ proposalId, accept, reason }) =>
      client.decideWorldProposal(
        proposalId,
        reason ? { accept, reason } : { accept },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: worldKeys.proposalLists(),
      });
      void queryClient.invalidateQueries({ queryKey: worldKeys.profiles() });
      void queryClient.invalidateQueries({
        queryKey: worldKeys.entityLists(),
      });
    },
  });
}
