"use client";

/**
 * TanStack Query hooks for the external PM-adapter surface (F18), over the
 * `/integrations/pm/connections` router.
 *
 * Kept in a dedicated module (like `sso.ts` / `marketplace.ts`) so the PM
 * integrations surface owns its own query keys + cache policy. Reads: the
 * connection list, one connection's detail (with per-state link tallies), and a
 * connection's links filtered to a sync state (the conflict inbox reads
 * `state="conflict"`). Mutations drive the connection lifecycle — connect, patch
 * (mapping / policy / enabled), disconnect, and the live health probe — each
 * seeding or invalidating the connection caches so the list, the detail header
 * and the health strip stay coherent the instant a change lands. Patch is
 * applied optimistically to the detail cache for a sub-100ms feel.
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
  PmConnection,
  PmConnectionConfigInput,
  PmConnectionDetail,
  PmConnectionPatch,
  PmHealthResult,
  PmLink,
  PmSyncState,
} from "./types";

export const pmKeys = {
  all: () => ["pm"] as const,
  connections: () => ["pm", "connections"] as const,
  connection: (connectionId: string) =>
    ["pm", "connection", connectionId] as const,
  links: (connectionId: string, state?: PmSyncState) =>
    ["pm", "links", connectionId, state ?? "all"] as const,
} as const;

/** Every external PM connection in the workspace. */
export function usePmConnections(
  client: ForgeApiClient = apiClient,
): UseQueryResult<PmConnection[]> {
  return useQuery({
    queryKey: pmKeys.connections(),
    queryFn: () => client.listPmConnections(),
  });
}

/** One connection's detail (config + per-state link tallies). */
export function usePmConnection(
  connectionId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PmConnectionDetail> {
  return useQuery({
    queryKey: pmKeys.connection(connectionId ?? ""),
    queryFn: () => client.getPmConnection(connectionId as string),
    enabled: Boolean(connectionId),
  });
}

/** A connection's links, optionally filtered to a sync state (conflict inbox). */
export function usePmLinks(
  connectionId: string | null,
  state: PmSyncState | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<PmLink[]> {
  return useQuery({
    queryKey: pmKeys.links(connectionId ?? "", state),
    queryFn: () => client.listPmLinks(connectionId as string, state),
    enabled: Boolean(connectionId),
  });
}

/** Connect a new PM adapter; seeds the list + selects the new connection. */
export function useCreatePmConnection(
  client: ForgeApiClient = apiClient,
): UseMutationResult<PmConnection, Error, PmConnectionConfigInput> {
  const queryClient = useQueryClient();
  return useMutation<PmConnection, Error, PmConnectionConfigInput>({
    mutationFn: (body) => client.createPmConnection(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: pmKeys.connections() });
    },
  });
}

export interface PatchPmConnectionVariables {
  connectionId: string;
  body: PmConnectionPatch;
}

/**
 * Patch a connection's mapping / policy / enabled flag. The detail cache is
 * updated optimistically so selects and the status-map editor feel instant;
 * a failure rolls back and a settle re-reads authoritative state.
 */
export function usePatchPmConnection(
  client: ForgeApiClient = apiClient,
): UseMutationResult<
  PmConnection,
  Error,
  PatchPmConnectionVariables,
  { previous?: PmConnectionDetail }
> {
  const queryClient = useQueryClient();
  return useMutation<
    PmConnection,
    Error,
    PatchPmConnectionVariables,
    { previous?: PmConnectionDetail }
  >({
    mutationFn: ({ connectionId, body }) =>
      client.patchPmConnection(connectionId, body),
    onMutate: async ({ connectionId, body }) => {
      const key = pmKeys.connection(connectionId);
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<PmConnectionDetail>(key);
      if (previous) {
        queryClient.setQueryData<PmConnectionDetail>(key, {
          ...previous,
          ...(body.name != null ? { name: body.name } : {}),
          ...(body.status_map != null ? { status_map: body.status_map } : {}),
          ...(body.sync_direction != null
            ? { sync_direction: body.sync_direction }
            : {}),
          ...(body.conflict_policy != null
            ? { conflict_policy: body.conflict_policy }
            : {}),
          ...(body.enabled != null
            ? { status: body.enabled ? "connected" : "disabled" }
            : {}),
        });
      }
      return { previous };
    },
    onError: (_err, { connectionId }, context) => {
      if (context?.previous) {
        queryClient.setQueryData(
          pmKeys.connection(connectionId),
          context.previous,
        );
      }
    },
    onSettled: (_data, _err, { connectionId }) => {
      void queryClient.invalidateQueries({
        queryKey: pmKeys.connection(connectionId),
      });
      void queryClient.invalidateQueries({ queryKey: pmKeys.connections() });
    },
  });
}

/** Disconnect a connection; revalidates the list + that connection's detail. */
export function useDisconnectPmConnection(
  client: ForgeApiClient = apiClient,
): UseMutationResult<PmConnection, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<PmConnection, Error, string>({
    mutationFn: (connectionId) => client.disconnectPmConnection(connectionId),
    onSuccess: (conn) => {
      queryClient.setQueryData<PmConnectionDetail | undefined>(
        pmKeys.connection(conn.id),
        (prev) => (prev ? { ...prev, ...conn } : prev),
      );
    },
    onSettled: (_data, _err, connectionId) => {
      void queryClient.invalidateQueries({ queryKey: pmKeys.connections() });
      void queryClient.invalidateQueries({
        queryKey: pmKeys.connection(connectionId),
      });
    },
  });
}

/**
 * Fire the live health probe. A mutation (not a query) so the "Test connection"
 * button can trigger it on demand and read the latency/scopes verdict; on
 * success it revalidates the connection so the header status + last-checked
 * timestamp reflect the probe.
 */
export function useTestPmConnection(
  client: ForgeApiClient = apiClient,
): UseMutationResult<PmHealthResult, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<PmHealthResult, Error, string>({
    mutationFn: (connectionId) => client.testPmConnection(connectionId),
    onSettled: (_data, _err, connectionId) => {
      void queryClient.invalidateQueries({
        queryKey: pmKeys.connection(connectionId),
      });
      void queryClient.invalidateQueries({ queryKey: pmKeys.connections() });
    },
  });
}
