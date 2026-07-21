"use client";

/**
 * TanStack Query hooks for the F23 spec-validation dashboard (over the F02
 * `/spec` engine).
 *
 * Kept in a dedicated module (rather than the board `hooks.ts`) so the spec
 * surface owns its own query keys + cache policy. `useApproveSpec` is
 * **optimistic** (spec UX standard: "state changes appear instantly, rollback
 * on error") — approving flips the spec's status to `approved` in every cached
 * dashboard the instant the reviewer acts, then revalidates on settle so the
 * engine's authoritative status (and any freshly generated tasks) win.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import { specStudioKeys } from "./spec-studio";
import type {
  ADR,
  AcceptanceCriterion,
  ExecutionMode,
  OpenQuestion,
  Requirement,
  SpecDashboard,
  SpecManifest,
  TaskDTO,
  ValidationReport,
} from "./types";

export const specKeys = {
  all: () => ["specs"] as const,
  overviews: () => ["specs", "overview"] as const,
  overview: (projectId: string) => ["specs", "overview", projectId] as const,
} as const;

/** The spec-validation dashboard payload for a project. */
export function useSpecOverview(
  projectId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SpecDashboard> {
  return useQuery({
    queryKey: specKeys.overview(projectId),
    queryFn: () => client.getProjectSpecOverview(projectId),
    enabled: Boolean(projectId),
  });
}

export interface CreateSpecVariables {
  epic_id: string;
  name: string;
  requirements?: Requirement[];
  /**
   * The rest of the Guided-mode form (Acceptance Criteria, Constraints,
   * Advanced section). `POST /spec/specs` only accepts
   * `epic_id`/`name`/`requirements`, so when any of these are set the
   * mutation follows up with a `PUT /spec/specs/{id}` to persist them —
   * otherwise anything the author filled in beyond requirements before
   * hitting "Create spec" would be silently dropped.
   */
  acceptance_criteria?: AcceptanceCriterion[];
  open_questions?: OpenQuestion[];
  constraints?: string[];
  decisions?: ADR[];
  execution_mode?: ExecutionMode;
  constitution_refs?: string[];
  repos?: string[];
}

function hasExtraGuidedFields(body: CreateSpecVariables): boolean {
  return Boolean(
    body.acceptance_criteria?.length ||
    body.open_questions?.length ||
    body.constraints?.length ||
    body.decisions?.length ||
    body.execution_mode ||
    body.constitution_refs?.length ||
    body.repos?.length,
  );
}

/**
 * Create a draft spec for an epic — the `/specs/new` entry point into the SDD
 * lifecycle. Guided mode collects the *whole* manifest (acceptance criteria,
 * constraints, execution mode, constitution refs, repos, decisions) before
 * the spec exists, but the create endpoint only takes
 * `epic_id`/`name`/`requirements`; when any of those extra fields are set,
 * this mutation follows the create with a `PUT /spec/specs/{id}` so nothing
 * the author drafted is lost.
 */
export function useCreateSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, CreateSpecVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateSpecVariables) => {
      const {
        acceptance_criteria,
        open_questions,
        constraints,
        decisions,
        execution_mode,
        constitution_refs,
        repos,
        ...createBody
      } = body;
      const created = await client.createSpec(createBody);
      if (!hasExtraGuidedFields(body)) {
        return created;
      }
      const fullManifest: SpecManifest = {
        ...created,
        acceptance_criteria,
        open_questions,
        constraints,
        decisions,
        execution_mode,
        constitution_refs,
        repos,
      };
      return client.putSpecManifest(created.id, fullManifest);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: specKeys.overviews() });
    },
  });
}

export interface ApproveSpecVariables {
  specId: string;
}

interface ApproveSpecContext {
  previous: [readonly unknown[], SpecDashboard | undefined][];
}

/**
 * Optimistic spec-approval mutation.
 *
 * `onMutate` snapshots every cached dashboard and flips the target spec's
 * status to `approved` so the lifecycle rail advances instantly; `onError`
 * restores the snapshots; `onSettled` revalidates from the engine.
 */
export function useApproveSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<
  SpecManifest,
  Error,
  ApproveSpecVariables,
  ApproveSpecContext
> {
  const queryClient = useQueryClient();
  return useMutation<
    SpecManifest,
    Error,
    ApproveSpecVariables,
    ApproveSpecContext
  >({
    mutationFn: ({ specId }) => client.approveSpec(specId),
    onMutate: async ({ specId }) => {
      await queryClient.cancelQueries({ queryKey: specKeys.overviews() });
      const previous = queryClient.getQueriesData<SpecDashboard>({
        queryKey: specKeys.overviews(),
      });
      queryClient.setQueriesData<SpecDashboard>(
        { queryKey: specKeys.overviews() },
        (old) =>
          old
            ? {
                ...old,
                specs: old.specs.map((spec) =>
                  spec.id === specId ? { ...spec, status: "approved" } : spec,
                ),
              }
            : old,
      );
      return { previous };
    },
    onError: (_error, _variables, context) => {
      if (!context) {
        return;
      }
      for (const [key, data] of context.previous) {
        queryClient.setQueryData(key, data);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: specKeys.all() });
    },
  });
}

export interface ReviewSpecVariables {
  specId: string;
  /** The reviewer's note — persisted server-side with the decision. */
  note: string;
}

interface ReviewSpecContext {
  previous: [readonly unknown[], SpecDashboard | undefined][];
  previousManifest: SpecManifest | undefined;
}

/**
 * Shared optimistic mutation for the two review decisions (reject /
 * request-changes). Mirrors `useApproveSpec`'s treatment — a review decision
 * is a simple status flip — but also flips the Spec Studio manifest cache so
 * Read mode reflects the decision instantly; `onError` rolls both back and
 * `onSettled` revalidates from the engine (the authoritative status +
 * persisted note win).
 */
function useReviewSpecDecision(
  client: ForgeApiClient,
  status: "rejected" | "changes_requested",
  send: (specId: string, note: string) => Promise<SpecManifest>,
): UseMutationResult<SpecManifest, Error, ReviewSpecVariables, ReviewSpecContext> {
  const queryClient = useQueryClient();
  return useMutation<SpecManifest, Error, ReviewSpecVariables, ReviewSpecContext>({
    mutationFn: ({ specId, note }) => send(specId, note),
    onMutate: async ({ specId, note }) => {
      await queryClient.cancelQueries({ queryKey: specKeys.overviews() });
      await queryClient.cancelQueries({ queryKey: specStudioKeys.manifest(specId) });
      const previous = optimisticallySetStatus(queryClient, specId, status);
      const previousManifest = queryClient.getQueryData<SpecManifest>(
        specStudioKeys.manifest(specId),
      );
      if (previousManifest) {
        queryClient.setQueryData<SpecManifest>(specStudioKeys.manifest(specId), {
          ...previousManifest,
          status,
          review_note: note || null,
        });
      }
      return { previous, previousManifest };
    },
    onError: (_error, { specId }, context) => {
      rollbackStatus(queryClient, context);
      if (context?.previousManifest) {
        queryClient.setQueryData(specStudioKeys.manifest(specId), context.previousManifest);
      }
    },
    onSettled: (_data, _error, { specId }) => invalidateSpecCaches(queryClient, specId),
  });
}

/** Reject a spec at the human gate (`POST /spec/specs/{id}/reject`). */
export function useRejectSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, ReviewSpecVariables, ReviewSpecContext> {
  return useReviewSpecDecision(client, "rejected", (specId, note) =>
    client.rejectSpec(specId, note),
  );
}

/** Request changes on a spec at the human gate (`POST /spec/specs/{id}/request-changes`). */
export function useRequestSpecChanges(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, ReviewSpecVariables, ReviewSpecContext> {
  return useReviewSpecDecision(client, "changes_requested", (specId, note) =>
    client.requestSpecChanges(specId, note),
  );
}

// --------------------------------------------------------------------------- //
// ss-lifecycle: the plain-language stepper's inline actions                   //
//                                                                              //
// Describe->Refine->Approve->Build->Verify, each backed by one existing       //
// `/spec` engine call (Clarify/Plan/Approve/Generate tasks/Validate — see     //
// `components/spec/spec-meta.ts`). Clarify is a simple, single-field status   //
// flip like Approve, so it gets the same optimistic treatment; Plan/Generate  //
// tasks/Validate touch richer manifest state (plans, tasks, gated validation) //
// that would be unsafe to fake, so those settle from the engine's response.   //
// --------------------------------------------------------------------------- //

interface SpecIdVariables {
  specId: string;
}

interface OptimisticStatusContext {
  previous: [readonly unknown[], SpecDashboard | undefined][];
}

/** Snapshot every cached dashboard and flip one spec's status ahead of the request. */
function optimisticallySetStatus(
  queryClient: ReturnType<typeof useQueryClient>,
  specId: string,
  status: SpecManifest["status"],
): OptimisticStatusContext["previous"] {
  const previous = queryClient.getQueriesData<SpecDashboard>({
    queryKey: specKeys.overviews(),
  });
  queryClient.setQueriesData<SpecDashboard>(
    { queryKey: specKeys.overviews() },
    (old) =>
      old
        ? {
            ...old,
            specs: old.specs.map((spec) => (spec.id === specId ? { ...spec, status } : spec)),
          }
        : old,
  );
  return previous;
}

function rollbackStatus(
  queryClient: ReturnType<typeof useQueryClient>,
  context: OptimisticStatusContext | undefined,
) {
  if (!context) return;
  for (const [key, data] of context.previous) {
    queryClient.setQueryData(key, data);
  }
}

/** After any lifecycle action settles, the studio's cached surfaces are stale. */
function invalidateSpecCaches(queryClient: ReturnType<typeof useQueryClient>, specId: string) {
  void queryClient.invalidateQueries({ queryKey: specKeys.all() });
  void queryClient.invalidateQueries({ queryKey: specStudioKeys.manifest(specId) });
  void queryClient.invalidateQueries({ queryKey: specStudioKeys.markdown(specId) });
  void queryClient.invalidateQueries({ queryKey: specStudioKeys.yaml(specId) });
}

/**
 * **Describe** step: run the clarification pass (`POST /spec/{id}/clarify`).
 * Optimistic — flips the spec to `clarifying` immediately, like `useApproveSpec`.
 */
export function useClarifySpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, SpecIdVariables, OptimisticStatusContext> {
  const queryClient = useQueryClient();
  return useMutation<SpecManifest, Error, SpecIdVariables, OptimisticStatusContext>({
    mutationFn: ({ specId }) => client.clarifySpec(specId),
    onMutate: async ({ specId }) => {
      await queryClient.cancelQueries({ queryKey: specKeys.overviews() });
      return { previous: optimisticallySetStatus(queryClient, specId, "clarifying") };
    },
    onError: (_error, _variables, context) => rollbackStatus(queryClient, context),
    onSettled: (_data, _error, { specId }) => invalidateSpecCaches(queryClient, specId),
  });
}

/**
 * **Refine** step: generate the technical plan + ADRs (`POST /spec/{id}/plan`).
 * Not optimistic — `plan_ref`/`decisions` are new manifest content, not a status
 * flip, so the UI waits for the engine's response.
 */
export function usePlanSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, SpecIdVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ specId }: SpecIdVariables) => client.planSpec(specId),
    onSettled: (_data, _error, { specId }) => invalidateSpecCaches(queryClient, specId),
  });
}

/**
 * **Build** step: generate implementation tasks from an approved spec
 * (`POST /spec/{id}/tasks`, 409 if not yet approved). Not optimistic — the
 * response is the task list, not the manifest, and the action is gated.
 */
export function useGenerateTasks(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TaskDTO[], Error, SpecIdVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ specId }: SpecIdVariables) => client.generateTasks(specId),
    onSettled: (_data, _error, { specId }) => invalidateSpecCaches(queryClient, specId),
  });
}

/**
 * **Verify** step: validate the spec's (deterministic) generated tasks
 * (`POST /spec/tasks/{task_id}/validate`). Task generation is idempotent, so
 * this re-runs `generateTasks` to resolve a task id rather than requiring the
 * Build step to have run first in this session.
 */
export function useValidateSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ValidationReport, Error, SpecIdVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ specId }: SpecIdVariables) => {
      const tasks = await client.generateTasks(specId);
      const taskId = tasks.find((task) => task.id)?.id;
      if (!taskId) {
        throw new Error("No tasks to validate yet — generate tasks first.");
      }
      return client.validateTask(taskId);
    },
    onSettled: (_data, _error, { specId }) => invalidateSpecCaches(queryClient, specId),
  });
}
