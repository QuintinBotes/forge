"use client";

/**
 * TanStack Query hooks for the Adaptive Orchestration settings surface
 * (`ao-settings-ui`), over the `/ao/role-config`, `/ao/settings` and
 * `/ao/routing-preview` routers.
 *
 * Kept in a dedicated module (like `sso.ts` / `deployments.ts`) so the AO
 * settings screen owns its query keys + cache policy. Reads: every role's
 * effective `{model_or_tier, effort}` and the workspace-wide auto-route
 * toggle / tier-model map / complexity thresholds. Mutations pin or clear a
 * per-role override and partially update the workspace settings, each
 * invalidating the relevant cache so the role table and the live routing
 * preview panel stay coherent. The routing preview itself is a mutation (a
 * probe fired imperatively as the sample-task form changes), not a query.
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
  AgentRole,
  AoSettingsOut,
  AoSettingsUpdateRequest,
  RoleConfigListResponse,
  RoleConfigOut,
  RoleConfigUpsertRequest,
  RoutingPreviewRequest,
  RoutingPreviewResponse,
  SelfEvalRunAccepted,
  SelfEvalStatusOut,
} from "./types";

export const aoSettingsKeys = {
  all: () => ["ao-settings"] as const,
  roleConfig: (projectId?: string) =>
    ["ao-settings", "role-config", projectId ?? null] as const,
  settings: () => ["ao-settings", "workspace-settings"] as const,
  selfEval: () => ["ao-settings", "self-eval"] as const,
} as const;

/** Every role's effective `{model_or_tier, effort}` (optionally project-scoped). */
export function useAoRoleConfig(
  projectId?: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<RoleConfigListResponse> {
  return useQuery({
    queryKey: aoSettingsKeys.roleConfig(projectId),
    queryFn: () => client.listAoRoleConfig(projectId),
  });
}

/** The workspace-wide auto-route toggle, tier-model map and complexity thresholds. */
export function useAoSettings(
  client: ForgeApiClient = apiClient,
): UseQueryResult<AoSettingsOut> {
  return useQuery({
    queryKey: aoSettingsKeys.settings(),
    queryFn: () => client.getAoSettings(),
  });
}

export interface UpsertAoRoleConfigVariables {
  role: AgentRole;
  body: RoleConfigUpsertRequest;
  projectId?: string;
}

/** Pin a workspace- or project-scoped override for one role, then revalidate. */
export function useUpsertAoRoleConfig(
  client: ForgeApiClient = apiClient,
): UseMutationResult<RoleConfigOut, Error, UpsertAoRoleConfigVariables> {
  const queryClient = useQueryClient();
  return useMutation<RoleConfigOut, Error, UpsertAoRoleConfigVariables>({
    mutationFn: ({ role, body, projectId }) =>
      client.upsertAoRoleConfig(role, body, projectId),
    onSettled: (_data, _err, { projectId }) => {
      void queryClient.invalidateQueries({
        queryKey: aoSettingsKeys.roleConfig(projectId),
      });
    },
  });
}

export interface DeleteAoRoleConfigVariables {
  role: AgentRole;
  projectId?: string;
}

/** Remove an override for one role, reverting to the next fallback. */
export function useDeleteAoRoleConfig(
  client: ForgeApiClient = apiClient,
): UseMutationResult<RoleConfigOut, Error, DeleteAoRoleConfigVariables> {
  const queryClient = useQueryClient();
  return useMutation<RoleConfigOut, Error, DeleteAoRoleConfigVariables>({
    mutationFn: ({ role, projectId }) => client.deleteAoRoleConfig(role, projectId),
    onSettled: (_data, _err, { projectId }) => {
      void queryClient.invalidateQueries({
        queryKey: aoSettingsKeys.roleConfig(projectId),
      });
    },
  });
}

/** Partially update the workspace-wide AO settings (auto-route, tier map, thresholds). */
export function useUpdateAoSettings(
  client: ForgeApiClient = apiClient,
): UseMutationResult<AoSettingsOut, Error, AoSettingsUpdateRequest> {
  const queryClient = useQueryClient();
  return useMutation<AoSettingsOut, Error, AoSettingsUpdateRequest>({
    mutationFn: (body) => client.updateAoSettings(body),
    onSuccess: (settings) => {
      queryClient.setQueryData(aoSettingsKeys.settings(), settings);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: aoSettingsKeys.settings() });
    },
  });
}

/**
 * What tier/model/strategy a sample task would get under the workspace's
 * current settings. A mutation (not a query) so the preview panel can fire it
 * on demand as the sample-task signals change.
 */
export function usePreviewAoRouting(
  client: ForgeApiClient = apiClient,
): UseMutationResult<RoutingPreviewResponse, Error, RoutingPreviewRequest> {
  return useMutation<RoutingPreviewResponse, Error, RoutingPreviewRequest>({
    mutationFn: (body) => client.previewAoRouting(body),
  });
}

/** Self-Eval Gate facts: enforcement flag, private suite, recorded baseline. */
export function useSelfEvalStatus(
  client: ForgeApiClient = apiClient,
): UseQueryResult<SelfEvalStatusOut> {
  return useQuery({
    queryKey: aoSettingsKeys.selfEval(),
    queryFn: () => client.getSelfEvalStatus(),
  });
}

/**
 * Queue the worker-owned `forge.self_eval.run` task (admin). Revalidates the
 * status read so a freshly recorded baseline eventually shows up on refetch.
 */
export function useRunSelfEval(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SelfEvalRunAccepted, Error, void> {
  const queryClient = useQueryClient();
  return useMutation<SelfEvalRunAccepted, Error, void>({
    mutationFn: () => client.runSelfEval(),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: aoSettingsKeys.selfEval() });
    },
  });
}
