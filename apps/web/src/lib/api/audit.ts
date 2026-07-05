"use client";

/**
 * TanStack Query hooks for the F39 immutable audit log.
 *
 * The log is read-only and cursor-paginated, so the listing is an
 * `useInfiniteQuery` keyed by its filter set — flattening `pages` yields the
 * accumulated rows and `fetchNextPage` walks `next_cursor`. `keepPreviousData`
 * keeps the current page on screen while a new filter combination loads, so
 * changing a filter swaps smoothly instead of flashing a skeleton.
 *
 * The filter vocabulary (`/audit/actions`) is effectively static per
 * deployment, so it is cached indefinitely and never retried. Chain
 * verification is a mutation (a POST that re-walks the hash chain) rather than a
 * query — it runs on demand from the toolbar.
 */

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  type UseInfiniteQueryResult,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  AuditListResponse,
  AuditQuery,
  AuditVocabulary,
  ChainVerifyResult,
} from "./types";

/** Filters that scope a listing (everything except the pagination cursor). */
export type AuditFilters = Omit<AuditQuery, "cursor">;

export const auditKeys = {
  all: () => ["audit"] as const,
  lists: () => ["audit", "list"] as const,
  list: (filters?: AuditFilters) => ["audit", "list", filters ?? {}] as const,
  vocabulary: () => ["audit", "vocabulary"] as const,
} as const;

/**
 * The immutable audit log as an infinite (cursor-paginated) query. Pass the
 * active filter set; the hook seeds each page's `cursor` from the prior page's
 * `next_cursor`.
 */
export function useAuditLog(
  filters?: AuditFilters,
  client: ForgeApiClient = apiClient,
): UseInfiniteQueryResult<
  { pages: AuditListResponse[]; pageParams: unknown[] },
  Error
> {
  return useInfiniteQuery({
    queryKey: auditKeys.list(filters),
    queryFn: ({ pageParam }) =>
      client.listAudit({ ...filters, cursor: pageParam ?? undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/**
 * The filter vocabulary (actions / actor types / resource types / outcomes /
 * severities). Static per deployment, so cached forever and not retried — a
 * failure simply leaves the selects to fall back to values seen in the rows.
 */
export function useAuditVocabulary(
  client: ForgeApiClient = apiClient,
): UseQueryResult<AuditVocabulary> {
  return useQuery({
    queryKey: auditKeys.vocabulary(),
    queryFn: () => client.getAuditVocabulary(),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
}

/** Re-walk the workspace's audit hash chain (integrity verification). */
export function useVerifyAuditChain(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ChainVerifyResult, Error, void> {
  return useMutation<ChainVerifyResult, Error, void>({
    mutationFn: () => client.verifyAuditChain(),
  });
}
