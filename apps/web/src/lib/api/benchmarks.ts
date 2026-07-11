"use client";

/**
 * TanStack Query hooks for the public benchmark leaderboard (F35 `/public`
 * router). Kept in a dedicated module (like the F32 marketplace, F39 audit)
 * so the leaderboard surface owns its own query keys. Both reads are
 * unauthenticated and GET-only; there is nothing to mutate from this surface.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type { PublicBenchmark, PublicLeaderboard } from "./types";

export const benchmarkKeys = {
  all: () => ["public-benchmarks"] as const,
  suites: () => ["public-benchmarks", "suites"] as const,
  leaderboard: (slug: string, version: string) =>
    ["public-benchmarks", "leaderboard", slug, version] as const,
} as const;

/** Every published benchmark suite (the leaderboard's suite picker). */
export function usePublicBenchmarks(
  client: ForgeApiClient = apiClient,
): UseQueryResult<PublicBenchmark[]> {
  return useQuery({
    queryKey: benchmarkKeys.suites(),
    queryFn: () => client.listPublicBenchmarks(),
  });
}

/** One suite's ranked, verified-first public leaderboard. */
export function usePublicLeaderboard(
  slug: string | null,
  version: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PublicLeaderboard> {
  return useQuery({
    queryKey: benchmarkKeys.leaderboard(slug ?? "", version ?? ""),
    queryFn: () => client.getPublicLeaderboard(slug as string, version as string),
    enabled: Boolean(slug && version),
  });
}
