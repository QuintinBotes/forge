"use client";

/**
 * TanStack Query hooks for Spec Studio's version history + diff (ss-versioning).
 *
 * A version is recorded on every save (Guided / Markdown / YAML — see
 * `lib/api/spec-studio.ts`'s `useSyncAfterSave`), so the history list here
 * invalidates whenever any of those three saves succeeds. Kept as its own
 * module (mirrors `spec.ts` / `spec-studio.ts`'s "own query keys" convention)
 * rather than folded into `spec-studio.ts`, since version history is a
 * read-only surface with no editor state to coordinate.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type { SpecVersionDetail, SpecVersionDiff, SpecVersionSummary } from "./types";

export const specVersionKeys = {
  list: (specId: string) => ["spec-versions", "list", specId] as const,
  detail: (specId: string, version: number) =>
    ["spec-versions", "detail", specId, version] as const,
  diff: (specId: string, from: number, to: number) =>
    ["spec-versions", "diff", specId, from, to] as const,
};

/** A spec's version history, newest first. */
export function useSpecVersions(
  specId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SpecVersionSummary[]> {
  return useQuery({
    queryKey: specVersionKeys.list(specId),
    queryFn: () => client.listSpecVersions(specId),
    enabled: Boolean(specId),
  });
}

/** One version's full snapshot (manifest + both serializations). */
export function useSpecVersion(
  specId: string,
  versionNumber: number | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SpecVersionDetail> {
  return useQuery({
    queryKey: specVersionKeys.detail(specId, versionNumber ?? -1),
    queryFn: () => client.getSpecVersion(specId, versionNumber as number),
    enabled: Boolean(specId) && versionNumber !== null,
  });
}

/** The diff between two versions of a spec. */
export function useSpecVersionDiff(
  specId: string,
  fromVersion: number | null,
  toVersion: number | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SpecVersionDiff> {
  return useQuery({
    queryKey: specVersionKeys.diff(specId, fromVersion ?? -1, toVersion ?? -1),
    queryFn: () => client.diffSpecVersions(specId, fromVersion as number, toVersion as number),
    enabled: Boolean(specId) && fromVersion !== null && toVersion !== null,
  });
}
