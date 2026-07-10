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
  CapacityReport,
  CFDSeries,
  CompleteSprintRequest,
  CycleLeadTimeReport,
  GoalAlignment,
  PortfolioVelocity,
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
  capacity: (sprintId: string) =>
    ["project-sprints", "capacity", sprintId] as const,
  goalAlignment: (sprintId: string) =>
    ["project-sprints", "goal-alignment", sprintId] as const,
  cfd: (projectId: string, start: string, end: string) =>
    ["project-sprints", "cfd", projectId, start, end] as const,
  cycleLeadTime: (projectId: string) =>
    ["project-sprints", "cycle-lead-time", projectId] as const,
  portfolioVelocity: (projectIds: string[], last: number) =>
    ["project-sprints", "portfolio-velocity", [...projectIds].sort(), last] as const,
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

/** Each member's declared capacity vs. their assigned committed-task points. */
export function useSprintCapacity(
  sprintId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<CapacityReport> {
  return useQuery({
    queryKey: sprintKeys.capacity(sprintId ?? ""),
    queryFn: () => client.getSprintCapacity(sprintId as string),
    enabled: Boolean(sprintId),
    placeholderData: keepPreviousData,
  });
}

/** The sprint goal's keyword coverage across its current tasks. */
export function useGoalAlignment(
  sprintId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<GoalAlignment> {
  return useQuery({
    queryKey: sprintKeys.goalAlignment(sprintId ?? ""),
    queryFn: () => client.getSprintGoalAlignment(sprintId as string),
    enabled: Boolean(sprintId),
    placeholderData: keepPreviousData,
  });
}

/** A project's Cumulative Flow Diagram over `[start, end]`. */
export function useProjectCfd(
  projectId: string | null | undefined,
  start: string,
  end: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<CFDSeries> {
  return useQuery({
    queryKey: sprintKeys.cfd(projectId ?? "", start, end),
    queryFn: () => client.getProjectCfd(projectId as string, start, end),
    enabled: Boolean(projectId && start && end),
    placeholderData: keepPreviousData,
  });
}

/** A project's per-task cycle/lead time + averages. */
export function useProjectCycleLeadTime(
  projectId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<CycleLeadTimeReport> {
  return useQuery({
    queryKey: sprintKeys.cycleLeadTime(projectId ?? ""),
    queryFn: () => client.getProjectCycleLeadTime(projectId as string),
    enabled: Boolean(projectId),
    placeholderData: keepPreviousData,
  });
}

/** Combined throughput/predictability trend across a set of projects. */
export function usePortfolioVelocity(
  projectIds: string[],
  last = 6,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PortfolioVelocity> {
  return useQuery({
    queryKey: sprintKeys.portfolioVelocity(projectIds, last),
    queryFn: () => client.getPortfolioVelocity(projectIds, last),
    enabled: projectIds.length > 0,
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
