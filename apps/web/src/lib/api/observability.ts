"use client";

/**
 * TanStack Query hooks for the F38 observability surface.
 *
 * The run-trace viewer reads a single agent run's step-level trace by id
 * (GET /observability/runs/{run_id}/trace). Retries are disabled so a 404
 * ("no trace recorded for that run") surfaces immediately as a not-found
 * state rather than spinning — the trace is immutable once recorded, so
 * retrying a missing id never helps.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type { RunTrace } from "./types";

export const observabilityKeys = {
  all: () => ["observability"] as const,
  runTrace: (runId: string) => ["observability", "run-trace", runId] as const,
} as const;

/** A single run's step-level trace; disabled until a run id is supplied. */
export function useRunTrace(
  runId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<RunTrace> {
  return useQuery({
    queryKey: observabilityKeys.runTrace(runId ?? ""),
    queryFn: () => client.getRunTrace(runId as string),
    enabled: Boolean(runId),
    retry: false,
  });
}
