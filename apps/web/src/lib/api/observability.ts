"use client";

/**
 * TanStack Query hooks for the F38 observability surface.
 *
 * The run-trace viewer reads a single agent run's step-level trace by id
 * (GET /observability/runs/{run_id}/trace). Retries are disabled so a 404
 * ("no trace recorded for that run") surfaces immediately as a not-found
 * state rather than spinning — the trace is immutable once recorded, so
 * retrying a missing id never helps.
 *
 * The Observability & cost dashboard adds three read hooks over the same
 * surface: grouped spend (`/cost/summary`), spend-over-time
 * (`/cost/timeseries`), and the derived retrieval-quality/latency view model
 * parsed from the Prometheus exposition (`/observability/metrics`).
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  parseRetrievalQuality,
  type RetrievalQuality,
} from "@/components/observability/observability-metrics";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  CostSummary,
  CostSummaryQuery,
  CostTimeseries,
  CostTimeseriesQuery,
  ReplayObjectiveInput,
  ReplayRunResult,
  RunTrace,
} from "./types";

export const observabilityKeys = {
  all: () => ["observability"] as const,
  runTrace: (runId: string) => ["observability", "run-trace", runId] as const,
  costSummary: (query?: CostSummaryQuery) =>
    ["observability", "cost", "summary", query ?? {}] as const,
  costTimeseries: (query?: CostTimeseriesQuery) =>
    ["observability", "cost", "timeseries", query ?? {}] as const,
  metrics: () => ["observability", "metrics"] as const,
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

/**
 * Time-Travel Runs: replay a persisted `RunRecording` cassette by
 * substitution (`POST /agent/runs/{run_id}/replay`) and report whether it
 * reproduced the tape. A mutation (an on-demand POST), not a query — it runs
 * from the run-trace view's replay control, keyed by the recording id.
 */
export function useReplayRun(
  runId: string,
  client: ForgeApiClient = apiClient,
): UseMutationResult<ReplayRunResult, Error, ReplayObjectiveInput> {
  return useMutation({
    mutationFn: (objective: ReplayObjectiveInput) => client.replayRun(runId, objective),
  });
}

/** Grouped spend for the current scope (breakdown by phase/provider/model). */
export function useCostSummary(
  query?: CostSummaryQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<CostSummary> {
  return useQuery({
    queryKey: observabilityKeys.costSummary(query),
    queryFn: () => client.getCostSummary(query),
    // Keep the prior breakdown on screen while a new dimension/window loads, so
    // switching Provider/Phase/Model swaps smoothly instead of flashing skeletons.
    placeholderData: keepPreviousData,
  });
}

/** Spend over time, one series per group key. */
export function useCostTimeseries(
  query?: CostTimeseriesQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<CostTimeseries> {
  return useQuery({
    queryKey: observabilityKeys.costTimeseries(query),
    queryFn: () => client.getCostTimeseries(query),
    placeholderData: keepPreviousData,
  });
}

/**
 * Retrieval-quality + latency view model, parsed from the Prometheus scrape.
 * Not retried (an empty body is a valid "observability disabled" answer, and a
 * transient parse never improves on retry) and kept briefly fresh.
 */
export function useObservabilityMetrics(
  client: ForgeApiClient = apiClient,
): UseQueryResult<RetrievalQuality> {
  return useQuery({
    queryKey: observabilityKeys.metrics(),
    queryFn: () => client.getMetricsExposition(),
    select: parseRetrievalQuality,
    retry: false,
    staleTime: 15_000,
  });
}
