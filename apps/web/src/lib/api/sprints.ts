"use client";

/**
 * TanStack Query hooks for the F26 sprints & velocity surface.
 *
 * Reads (sprint list, velocity dashboard, burndown) are keyed under the shared
 * `["project-sprints"]` root so a single `invalidateQueries` after a lifecycle
 * mutation refreshes the whole screen. The start/complete mutations advance the
 * sprint FSM (planned -> active -> completed); on success they invalidate the
 * sprint reads *and* the board `["tasks"]` cache, because completing a sprint
 * routes carryover tasks back to the backlog.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  BurndownSeries,
  CompleteSprintRequest,
  Sprint,
  SprintReport,
  VelocityDashboard,
} from "./types";

export const sprintKeys = {
  all: () => ["project-sprints"] as const,
  list: (projectId: string) => ["project-sprints", "list", projectId] as const,
  velocity: (projectId: string, last: number) =>
    ["project-sprints", "velocity", projectId, last] as const,
  burndown: (sprintId: string) =>
    ["project-sprints", "burndown", sprintId] as const,
  report: (sprintId: string) =>
    ["project-sprints", "report", sprintId] as const,
} as const;

/** Every sprint for a project (planned / active / completed / cancelled). */
export function useProjectSprints(
  projectId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<Sprint[]> {
  return useQuery({
    queryKey: sprintKeys.list(projectId),
    queryFn: () => client.listProjectSprints(projectId),
    enabled: Boolean(projectId),
  });
}

/** Committed-vs-completed velocity + forecast over the last `last` sprints. */
export function useVelocityDashboard(
  projectId: string,
  last = 6,
  client: ForgeApiClient = apiClient,
): UseQueryResult<VelocityDashboard> {
  return useQuery({
    queryKey: sprintKeys.velocity(projectId, last),
    queryFn: () => client.getVelocityDashboard(projectId, last),
    enabled: Boolean(projectId),
    placeholderData: keepPreviousData,
  });
}

/**
 * A sprint's day-by-day burndown. Disabled until a sprint id is supplied and
 * kept on screen while switching sprints so the chart swaps without a flash.
 */
export function useSprintBurndown(
  sprintId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<BurndownSeries> {
  return useQuery({
    queryKey: sprintKeys.burndown(sprintId ?? ""),
    queryFn: () => client.getSprintBurndown(sprintId as string),
    enabled: Boolean(sprintId),
    placeholderData: keepPreviousData,
  });
}

/** Start a planned sprint; revalidates the sprint reads + the board. */
export function useStartSprint(
  client: ForgeApiClient = apiClient,
): UseMutationResult<Sprint, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sprintId: string) => client.startSprint(sprintId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: sprintKeys.all() });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export interface CompleteSprintVariables {
  sprintId: string;
  body?: CompleteSprintRequest;
}

/** Complete an active sprint (routes carryover); revalidates reads + board. */
export function useCompleteSprint(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SprintReport, Error, CompleteSprintVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sprintId, body }: CompleteSprintVariables) =>
      client.completeSprint(sprintId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: sprintKeys.all() });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
