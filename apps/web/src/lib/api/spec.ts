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
import type { SpecDashboard, SpecManifest } from "./types";

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
): UseMutationResult<SpecManifest, Error, ApproveSpecVariables, ApproveSpecContext> {
  const queryClient = useQueryClient();
  return useMutation<SpecManifest, Error, ApproveSpecVariables, ApproveSpecContext>({
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
