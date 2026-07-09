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
import type {
  BulkUpdate,
  EpicDTO,
  IncidentDTO,
  MilestoneDTO,
  Principal,
  SprintDTO,
  TaskDTO,
  TaskStatus,
} from "./types";

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
  sprints: () => ["sprints"] as const,
  milestones: () => ["milestones"] as const,
  incidents: () => ["incidents"] as const,
  currentUser: () => ["me"] as const,
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

/**
 * Create an epic. Used by the standalone `/specs/new` entry point when the
 * author starts from the `/specs` dashboard empty state with no epic yet to
 * pick — it creates the epic, then the spec underneath it.
 */
export function useCreateEpic(
  client: ForgeApiClient = apiClient,
): UseMutationResult<EpicDTO, Error, EpicDTO> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (epic: EpicDTO) => client.createEpic(epic),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.epics() });
    },
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

export function useSprints(
  client: ForgeApiClient = apiClient,
): UseQueryResult<SprintDTO[]> {
  return useQuery({
    queryKey: queryKeys.sprints(),
    queryFn: () => client.listSprints(),
  });
}

export function useMilestones(
  client: ForgeApiClient = apiClient,
): UseQueryResult<MilestoneDTO[]> {
  return useQuery({
    queryKey: queryKeys.milestones(),
    queryFn: () => client.listMilestones(),
  });
}

/**
 * The authenticated principal. Never retried and quietly `null` on failure so an
 * unauthenticated/offline session degrades to "assign to me" being disabled
 * rather than surfacing an error on the board.
 */
export function useCurrentUser(
  client: ForgeApiClient = apiClient,
): UseQueryResult<Principal> {
  return useQuery({
    queryKey: queryKeys.currentUser(),
    queryFn: () => client.me(),
    retry: false,
    staleTime: 5 * 60_000,
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

export interface UpdateTaskVariables {
  taskId: string;
  patch: Partial<TaskDTO>;
}

/** Patch a single task (e.g. reassign) and revalidate the board. */
export function useUpdateTask(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TaskDTO, Error, UpdateTaskVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, patch }: UpdateTaskVariables) =>
      client.updateTask(taskId, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

interface BulkUpdateContext {
  previousLists: [readonly unknown[], TaskDTO[] | undefined][];
}

/** Fields a {@link BulkUpdate} can optimistically patch onto a cached task. */
function applyBulkPatch(task: TaskDTO, update: BulkUpdate): TaskDTO {
  const next: TaskDTO = { ...task };
  if (update.status !== undefined) next.status = update.status;
  if (update.priority !== undefined) next.priority = update.priority;
  if (update.assignee_id !== undefined) next.assignee_id = update.assignee_id;
  if (update.sprint_id !== undefined) next.sprint_id = update.sprint_id;
  if (update.labels !== undefined) next.labels = update.labels;
  return next;
}

/**
 * Optimistic bulk mutation (spec: bulk actions). Patches every cached task list
 * immediately, rolls the snapshots back on error, and revalidates on settle.
 */
export function useBulkUpdateTasks(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TaskDTO[], Error, BulkUpdate[], BulkUpdateContext> {
  const queryClient = useQueryClient();
  return useMutation<TaskDTO[], Error, BulkUpdate[], BulkUpdateContext>({
    mutationFn: (updates: BulkUpdate[]) => client.bulkUpdateTasks(updates),
    onMutate: async (updates) => {
      await queryClient.cancelQueries({ queryKey: ["tasks"] });
      const byId = new Map(updates.map((u) => [u.task_id, u]));
      const previousLists = queryClient.getQueriesData<TaskDTO[]>({
        queryKey: queryKeys.taskLists(),
      });
      queryClient.setQueriesData<TaskDTO[]>(
        { queryKey: queryKeys.taskLists() },
        (old) =>
          old?.map((task) => {
            const update = task.id ? byId.get(task.id) : undefined;
            return update ? applyBulkPatch(task, update) : task;
          }),
      );
      return { previousLists };
    },
    onError: (_error, _updates, context) => {
      if (!context) {
        return;
      }
      for (const [key, data] of context.previousLists) {
        queryClient.setQueryData(key, data);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
