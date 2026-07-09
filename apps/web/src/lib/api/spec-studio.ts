"use client";

/**
 * TanStack Query hooks for Spec Studio — the spec-authoring surface over one
 * canonical `SpecManifest`, editable from four modes (Guided / Markdown /
 * YAML / Read; see `components/spec-studio/spec-studio.tsx`). Mirrors the
 * `lib/api/spec.ts` convention: dedicated query keys, an injectable client.
 *
 * `spec.md` and `manifest.yaml` are lazily fetched (only once their mode is
 * visited) and share one invariant: saving *any* of the three editable
 * surfaces re-renders the other two on the backend, so a successful save
 * invalidates the sibling queries rather than trusting stale cached text.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import { specVersionKeys } from "./spec-versions";
import type { SpecDraft, SpecImport, SpecManifest } from "./types";

export const specStudioKeys = {
  manifest: (specId: string) => ["spec-studio", "manifest", specId] as const,
  markdown: (specId: string) => ["spec-studio", "markdown", specId] as const,
  yaml: (specId: string) => ["spec-studio", "yaml", specId] as const,
};

export function useSpecStudioManifest(
  specId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SpecManifest> {
  return useQuery({
    queryKey: specStudioKeys.manifest(specId),
    queryFn: () => client.getSpecManifest(specId),
    enabled: Boolean(specId),
  });
}

export function useSpecStudioMarkdown(
  specId: string,
  enabled: boolean,
  client: ForgeApiClient = apiClient,
): UseQueryResult<string> {
  return useQuery({
    queryKey: specStudioKeys.markdown(specId),
    queryFn: () => client.getSpecMarkdown(specId),
    enabled: Boolean(specId) && enabled,
  });
}

export function useSpecStudioYaml(
  specId: string,
  enabled: boolean,
  client: ForgeApiClient = apiClient,
): UseQueryResult<string> {
  return useQuery({
    queryKey: specStudioKeys.yaml(specId),
    queryFn: () => client.getSpecManifestYaml(specId),
    enabled: Boolean(specId) && enabled,
  });
}

/** After any save, the manifest cache gets the fresh value; siblings just refetch. */
function useSyncAfterSave(specId: string) {
  const queryClient = useQueryClient();
  return (updated: SpecManifest, savedFrom: "guided" | "markdown" | "yaml") => {
    queryClient.setQueryData(specStudioKeys.manifest(specId), updated);
    if (savedFrom !== "markdown") {
      void queryClient.invalidateQueries({ queryKey: specStudioKeys.markdown(specId) });
    }
    if (savedFrom !== "yaml") {
      void queryClient.invalidateQueries({ queryKey: specStudioKeys.yaml(specId) });
    }
    // ss-versioning: every save records a new version; refresh the history list.
    void queryClient.invalidateQueries({ queryKey: specVersionKeys.list(specId) });
  };
}

export function useSaveGuidedManifest(
  specId: string,
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, SpecManifest> {
  const sync = useSyncAfterSave(specId);
  return useMutation({
    mutationFn: (manifest: SpecManifest) => client.putSpecManifest(specId, manifest),
    onSuccess: (updated) => sync(updated, "guided"),
  });
}

export function useSaveSpecMarkdown(
  specId: string,
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, string> {
  const sync = useSyncAfterSave(specId);
  return useMutation({
    mutationFn: (content: string) => client.putSpecMarkdown(specId, content),
    onSuccess: (updated) => sync(updated, "markdown"),
  });
}

export function useSaveSpecManifestYaml(
  specId: string,
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecManifest, Error, string> {
  const sync = useSyncAfterSave(specId);
  return useMutation({
    mutationFn: (content: string) => client.putSpecManifestYaml(specId, content),
    onSuccess: (updated) => sync(updated, "yaml"),
  });
}

export interface DraftSpecVariables {
  goal: string;
  epic_id?: string;
  project_id?: string;
}

/**
 * `ss-ai-panel`: draft a `spec.md` from a one-line goal (`POST /spec/draft`).
 * Draft-only — nothing is persisted or cached; the caller (`AiDraftPanel`)
 * owns streaming the result into the Guided/Markdown editor.
 */
export function useDraftSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecDraft, Error, DraftSpecVariables> {
  return useMutation({
    mutationFn: (body: DraftSpecVariables) => client.draftSpec(body),
  });
}

export interface ImportSpecVariables {
  content: string;
  source_format?: "markdown" | "yaml" | "auto";
}

/**
 * `ss-import`: import an existing markdown or YAML spec (pasted/uploaded from
 * outside Forge) as a `spec.md` draft (`POST /spec/import`). Draft-only —
 * nothing is persisted or cached; the caller reviews/refines the result in the
 * Markdown or Guided editor before saving, mirroring `useDraftSpec`.
 */
export function useImportSpec(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SpecImport, Error, ImportSpecVariables> {
  return useMutation({
    mutationFn: (body: ImportSpecVariables) => client.importSpec(body),
  });
}
