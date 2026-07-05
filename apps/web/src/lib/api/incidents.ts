"use client";

/**
 * TanStack Query hooks for the F17 incident-workflow surface.
 *
 * Kept in a dedicated module (like the F36 approval inbox) so the incident
 * surface owns its own query keys and cache-invalidation policy. Every driving
 * mutation — declaring an incident, sending an FSM event, publishing a
 * postmortem — invalidates the touched incident's caches on settle so the
 * lifecycle badge, timeline, remediation plan and postmortem stay coherent.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, apiClient, type ForgeApiClient } from "./client";
import type {
  IncidentDeclareRequest,
  IncidentDetailView,
  IncidentEventRequest,
  IncidentEventView,
  IncidentView,
  PostmortemView,
  RemediationPlanView,
} from "./types";

export const incidentKeys = {
  all: () => ["incidents"] as const,
  lists: () => ["incidents", "list"] as const,
  list: (filters?: Record<string, unknown>) =>
    ["incidents", "list", filters ?? {}] as const,
  detail: (incidentId: string) => ["incidents", "detail", incidentId] as const,
  timeline: (incidentId: string) =>
    ["incidents", "timeline", incidentId] as const,
  remediation: (incidentId: string) =>
    ["incidents", "remediation", incidentId] as const,
  postmortem: (incidentId: string) =>
    ["incidents", "postmortem", incidentId] as const,
} as const;

export type IncidentFilters = Record<
  string,
  string | number | boolean | undefined
>;

/** The incident queue (optionally filtered by state / severity / project). */
export function useIncidents(
  filters?: IncidentFilters,
  client: ForgeApiClient = apiClient,
): UseQueryResult<IncidentView[]> {
  return useQuery({
    queryKey: incidentKeys.list(filters),
    queryFn: () => client.listIncidentRecords(filters),
  });
}

/** One incident's detail (summary + latest remediation plan + event count). */
export function useIncidentDetail(
  incidentId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<IncidentDetailView> {
  return useQuery({
    queryKey: incidentKeys.detail(incidentId ?? ""),
    queryFn: () => client.getIncident(incidentId as string),
    enabled: Boolean(incidentId),
  });
}

/** The ordered incident timeline. */
export function useIncidentTimeline(
  incidentId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<IncidentEventView[]> {
  return useQuery({
    queryKey: incidentKeys.timeline(incidentId ?? ""),
    queryFn: () => client.getIncidentTimeline(incidentId as string),
    enabled: Boolean(incidentId),
  });
}

/**
 * The latest proposed remediation plan. A 404 (no plan proposed yet) is a
 * normal empty state, not an error, so it resolves to `null` rather than
 * throwing — the panel renders its "no plan yet" guidance instead of an error.
 */
export function useRemediationPlan(
  incidentId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<RemediationPlanView | null> {
  return useQuery({
    queryKey: incidentKeys.remediation(incidentId ?? ""),
    queryFn: async () => {
      try {
        return await client.getRemediationPlan(incidentId as string);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(incidentId),
  });
}

/**
 * The incident's postmortem + action items. Like the remediation plan, a 404
 * (no postmortem until the incident is resolved) resolves to `null`.
 */
export function usePostmortem(
  incidentId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PostmortemView | null> {
  return useQuery({
    queryKey: incidentKeys.postmortem(incidentId ?? ""),
    queryFn: async () => {
      try {
        return await client.getPostmortem(incidentId as string);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(incidentId),
  });
}

/** Declare a manual incident; refreshes the queue on settle. */
export function useDeclareIncident(
  client: ForgeApiClient = apiClient,
): UseMutationResult<IncidentView, Error, IncidentDeclareRequest> {
  const queryClient = useQueryClient();
  return useMutation<IncidentView, Error, IncidentDeclareRequest>({
    mutationFn: (body) => client.declareIncident(body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: incidentKeys.lists() });
    },
  });
}

export interface SendEventVariables {
  incidentId: string;
  body: IncidentEventRequest;
}

/**
 * Drive an incident's FSM with an event. On success we seed the detail cache
 * with the returned view and invalidate the incident's timeline/remediation +
 * the queue so the lifecycle badge and action bar advance immediately.
 */
export function useSendIncidentEvent(
  client: ForgeApiClient = apiClient,
): UseMutationResult<IncidentDetailView, Error, SendEventVariables> {
  const queryClient = useQueryClient();
  return useMutation<IncidentDetailView, Error, SendEventVariables>({
    mutationFn: ({ incidentId, body }) =>
      client.sendIncidentEvent(incidentId, body),
    onSuccess: (detail, { incidentId }) => {
      queryClient.setQueryData(incidentKeys.detail(incidentId), detail);
    },
    onSettled: (_data, _error, { incidentId }) => {
      void queryClient.invalidateQueries({ queryKey: incidentKeys.lists() });
      void queryClient.invalidateQueries({
        queryKey: incidentKeys.timeline(incidentId),
      });
      void queryClient.invalidateQueries({
        queryKey: incidentKeys.remediation(incidentId),
      });
      void queryClient.invalidateQueries({
        queryKey: incidentKeys.postmortem(incidentId),
      });
    },
  });
}

/** Publish an incident's postmortem; refreshes the postmortem cache on settle. */
export function usePublishPostmortem(
  client: ForgeApiClient = apiClient,
): UseMutationResult<PostmortemView, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<PostmortemView, Error, string>({
    mutationFn: (incidentId) => client.publishPostmortem(incidentId),
    onSuccess: (postmortem, incidentId) => {
      queryClient.setQueryData(incidentKeys.postmortem(incidentId), postmortem);
    },
    onSettled: (_data, _error, incidentId) => {
      void queryClient.invalidateQueries({
        queryKey: incidentKeys.postmortem(incidentId),
      });
    },
  });
}
