"use client";

/**
 * TanStack Query hooks over the typed {@link ForgeApiClient}.
 *
 * Phase-0 substrate: query keys + a small set of board read hooks plus a status
 * mutation. Task 1.6 extends these (optimistic updates, WS invalidation, etc.).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type { EpicDTO, IncidentDTO, TaskDTO, TaskStatus } from "./types";

export const queryKeys = {
  tasks: (filters?: Record<string, unknown>) =>
    ["tasks", filters ?? {}] as const,
  task: (taskId: string) => ["tasks", taskId] as const,
  epics: () => ["epics"] as const,
  incidents: () => ["incidents"] as const,
} as const;

export function useTasks(
  filters?: Record<string, string | number | boolean | undefined>,
  client: ForgeApiClient = apiClient,
): UseQueryResult<TaskDTO[]> {
  return useQuery({
    queryKey: queryKeys.tasks(filters),
    queryFn: () => client.listTasks(filters),
  });
}

export function useTask(
  taskId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<TaskDTO> {
  return useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => client.getTask(taskId),
    enabled: Boolean(taskId),
  });
}

export function useEpics(
  client: ForgeApiClient = apiClient,
): UseQueryResult<EpicDTO[]> {
  return useQuery({
    queryKey: queryKeys.epics(),
    queryFn: () => client.listEpics(),
  });
}

export function useIncidents(
  client: ForgeApiClient = apiClient,
): UseQueryResult<IncidentDTO[]> {
  return useQuery({
    queryKey: queryKeys.incidents(),
    queryFn: () => client.listIncidents(),
  });
}

export interface SetTaskStatusVariables {
  taskId: string;
  status: TaskStatus;
}

export function useSetTaskStatus(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TaskDTO, Error, SetTaskStatusVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, status }: SetTaskStatusVariables) =>
      client.setTaskStatus(taskId, status),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.task(variables.taskId),
      });
    },
  });
}
