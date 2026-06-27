"use client";

/**
 * TanStack Query hooks over the typed {@link ForgeApiClient}.
 *
 * Board surface for Task 1.6: read hooks (tasks/epics/incidents), a create
 * mutation, and an **optimistic** status mutation that updates the cache
 * immediately and rolls back on error (spec UX standard: "state changes appear
 * instantly, rollback on error").
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

/**
 * Query keys. Lists and detail share the `["tasks"]` root so a single
 * `invalidateQueries({ queryKey: ["tasks"] })` refreshes everything, but use
 * distinct `"list"` / `"detail"` segments so optimistic writes can target the
 * (array-shaped) lists without clobbering the (object-shaped) detail entry.
 */
export const queryKeys = {
  tasks: (filters?: Record<string, unknown>) =>
    ["tasks", "list", filters ?? {}] as const,
  taskLists: () => ["tasks", "list"] as const,
  task: (taskId: string) => ["tasks", "detail", taskId] as const,
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

export function useCreateTask(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TaskDTO, Error, TaskDTO> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (task: TaskDTO) => client.createTask(task),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export interface SetTaskStatusVariables {
  taskId: string;
  status: TaskStatus;
}

interface SetTaskStatusContext {
  previousLists: [readonly unknown[], TaskDTO[] | undefined][];
  previousDetail: TaskDTO | undefined;
  taskId: string;
}

/**
 * Optimistic status mutation.
 *
 * `onMutate` snapshots and patches every cached task list (and the detail
 * entry) so the UI updates instantly; `onError` restores the snapshots;
 * `onSettled` revalidates from the server.
 */
export function useSetTaskStatus(
  client: ForgeApiClient = apiClient,
): UseMutationResult<
  TaskDTO,
  Error,
  SetTaskStatusVariables,
  SetTaskStatusContext
> {
  const queryClient = useQueryClient();
  return useMutation<
    TaskDTO,
    Error,
    SetTaskStatusVariables,
    SetTaskStatusContext
  >({
    mutationFn: ({ taskId, status }) => client.setTaskStatus(taskId, status),
    onMutate: async ({ taskId, status }) => {
      // Cancel in-flight refetches so they can't overwrite the optimistic value.
      await queryClient.cancelQueries({ queryKey: ["tasks"] });

      const previousLists = queryClient.getQueriesData<TaskDTO[]>({
        queryKey: queryKeys.taskLists(),
      });
      const previousDetail = queryClient.getQueryData<TaskDTO>(
        queryKeys.task(taskId),
      );

      queryClient.setQueriesData<TaskDTO[]>(
        { queryKey: queryKeys.taskLists() },
        (old) =>
          old?.map((task) =>
            task.id === taskId ? { ...task, status } : task,
          ),
      );
      if (previousDetail) {
        queryClient.setQueryData<TaskDTO>(queryKeys.task(taskId), {
          ...previousDetail,
          status,
        });
      }

      return { previousLists, previousDetail, taskId };
    },
    onError: (_error, _variables, context) => {
      if (!context) {
        return;
      }
      for (const [key, data] of context.previousLists) {
        queryClient.setQueryData(key, data);
      }
      if (context.previousDetail) {
        queryClient.setQueryData(
          queryKeys.task(context.taskId),
          context.previousDetail,
        );
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
