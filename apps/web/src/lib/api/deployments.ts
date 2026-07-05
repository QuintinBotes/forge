"use client";

/**
 * TanStack Query hooks for the F31 deployment-gates surface (over the
 * `/deployments` + `/projects/{id}/pipeline` routers).
 *
 * Kept in a dedicated module (like `incidents.ts` / `sprints.ts`) so the
 * deployment surface owns its query keys + cache policy. Reads: the promotion
 * pipeline, the recent-deployments list, and one deployment's gate detail.
 * Mutations drive the promotion FSM — request a promotion, decide a gate,
 * cancel, roll back — and each invalidates the whole `["deployments"]` root so
 * the pipeline's "currently deployed" markers, the list, and the open detail
 * stay coherent the moment a gate clears.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, apiClient, type ForgeApiClient } from "./client";
import type {
  DeploymentDecisionRequest,
  DeploymentDetail,
  DeploymentListQuery,
  DeploymentRead,
  DeploymentRequestBody,
  PipelineRead,
} from "./types";

export const deploymentKeys = {
  all: () => ["deployments"] as const,
  pipeline: (projectId: string) =>
    ["deployments", "pipeline", projectId] as const,
  lists: () => ["deployments", "list"] as const,
  list: (projectId: string, filters?: DeploymentListQuery) =>
    ["deployments", "list", projectId, filters ?? {}] as const,
  detail: (deploymentId: string) =>
    ["deployments", "detail", deploymentId] as const,
} as const;

/**
 * A project's promotion pipeline (ranked environments + what's live on each).
 * A 404 (no pipeline configured yet) is a normal empty state, not an error, so
 * it resolves to `null` — the screen renders its "configure a pipeline" guide.
 */
export function useDeploymentPipeline(
  projectId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PipelineRead | null> {
  return useQuery({
    queryKey: deploymentKeys.pipeline(projectId),
    queryFn: async () => {
      try {
        return await client.getDeploymentPipeline(projectId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(projectId),
  });
}

/** Recent deployments for a project (optionally by environment / state). */
export function useProjectDeployments(
  projectId: string,
  filters?: DeploymentListQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<DeploymentRead[]> {
  return useQuery({
    queryKey: deploymentKeys.list(projectId, filters),
    queryFn: () => client.listProjectDeployments(projectId, filters),
    enabled: Boolean(projectId),
    placeholderData: keepPreviousData,
  });
}

/**
 * One deployment's gate detail: its gate evaluation, per-check results and the
 * transition history. Disabled until a deployment id is supplied and kept on
 * screen while switching selections so the panel swaps without a flash.
 */
export function useDeploymentDetail(
  deploymentId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<DeploymentDetail> {
  return useQuery({
    queryKey: deploymentKeys.detail(deploymentId ?? ""),
    queryFn: () => client.getDeployment(deploymentId as string),
    enabled: Boolean(deploymentId),
    placeholderData: keepPreviousData,
  });
}

export interface RequestDeploymentVariables {
  projectId: string;
  body: DeploymentRequestBody;
}

/** Request a promotion of a commit to an environment; refreshes the pipeline. */
export function useRequestDeployment(
  client: ForgeApiClient = apiClient,
): UseMutationResult<DeploymentRead, Error, RequestDeploymentVariables> {
  const queryClient = useQueryClient();
  return useMutation<DeploymentRead, Error, RequestDeploymentVariables>({
    mutationFn: ({ projectId, body }) => client.requestDeployment(projectId, body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: deploymentKeys.all() });
    },
  });
}

export interface DecideDeploymentVariables {
  deploymentId: string;
  body: DeploymentDecisionRequest;
}

/** Approve / reject / request-changes on a gated deployment. */
export function useDecideDeployment(
  client: ForgeApiClient = apiClient,
): UseMutationResult<DeploymentRead, Error, DecideDeploymentVariables> {
  const queryClient = useQueryClient();
  return useMutation<DeploymentRead, Error, DecideDeploymentVariables>({
    mutationFn: ({ deploymentId, body }) =>
      client.decideDeployment(deploymentId, body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: deploymentKeys.all() });
    },
  });
}

/** Cancel an in-flight deployment; refreshes the pipeline + list + detail. */
export function useCancelDeployment(
  client: ForgeApiClient = apiClient,
): UseMutationResult<DeploymentRead, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<DeploymentRead, Error, string>({
    mutationFn: (deploymentId) => client.cancelDeployment(deploymentId),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: deploymentKeys.all() });
    },
  });
}

/** Roll back a succeeded deployment; refreshes the pipeline + list + detail. */
export function useRollbackDeployment(
  client: ForgeApiClient = apiClient,
): UseMutationResult<DeploymentRead, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<DeploymentRead, Error, string>({
    mutationFn: (deploymentId) => client.rollbackDeployment(deploymentId),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: deploymentKeys.all() });
    },
  });
}
