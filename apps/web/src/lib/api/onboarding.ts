"use client";

/**
 * TanStack Query hook for the guided walkthrough's onboarding progress.
 *
 * Reads the derived {@link OnboardingProgress} over the typed
 * {@link ForgeApiClient} (which composes the specs / approvals / deployments
 * router reads). Kept in its own module — like `spec.ts` and `marketplace.ts` —
 * so the walkthrough surface owns its query key + cache policy.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type { OnboardingProgress } from "./types";

export const onboardingKeys = {
  all: () => ["onboarding"] as const,
  progress: (projectId: string) =>
    ["onboarding", "progress", projectId] as const,
} as const;

/**
 * Live progress through the build loop for a project. Disabled until a project
 * id is known; refetched on focus is off (this is a slow-moving projection).
 */
export function useOnboardingProgress(
  projectId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<OnboardingProgress> {
  return useQuery({
    queryKey: onboardingKeys.progress(projectId),
    queryFn: () => client.getOnboardingProgress(projectId),
    enabled: Boolean(projectId),
    staleTime: 30_000,
  });
}
