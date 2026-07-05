"use client";

/**
 * TanStack Query hooks for the F28 workflow visual editor (over the
 * `/workflow/editor` router).
 *
 * Reads: the registry catalog (palette), the definition list, and one
 * definition's detail (its published + draft graphs and validation issues).
 * Mutations drive the governed authoring lifecycle — create, fork, save-draft,
 * validate, publish — and each invalidates the `["workflow"]` root so the rail's
 * draft/published badges and the open definition stay coherent the moment a
 * draft is saved or a revision is published.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  CreateWorkflowDefinition,
  SaveWorkflowDraftRequest,
  WorkflowCatalog,
  WorkflowDefinitionDetail,
  WorkflowDefinitionSummary,
  WorkflowRevisionDetail,
  WorkflowValidationIssue,
} from "./types";

export const workflowKeys = {
  all: () => ["workflow"] as const,
  catalog: () => ["workflow", "catalog"] as const,
  definitions: () => ["workflow", "definitions"] as const,
  definition: (name: string) => ["workflow", "definition", name] as const,
} as const;

/** The registry palette the editor composes transitions from (states/guards/…). */
export function useWorkflowCatalog(
  client: ForgeApiClient = apiClient,
): UseQueryResult<WorkflowCatalog> {
  return useQuery({
    queryKey: workflowKeys.catalog(),
    queryFn: () => client.getWorkflowCatalog(),
    staleTime: 5 * 60_000,
  });
}

/** Every workflow definition in the workspace (bundled + custom + forks). */
export function useWorkflowDefinitions(
  client: ForgeApiClient = apiClient,
): UseQueryResult<WorkflowDefinitionSummary[]> {
  return useQuery({
    queryKey: workflowKeys.definitions(),
    queryFn: () => client.listWorkflowDefinitions(),
  });
}

/**
 * One definition's detail (its published + draft graphs). Disabled until a name
 * is chosen and kept on screen while switching selections so the canvas swaps
 * without a flash.
 */
export function useWorkflowDefinition(
  name: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<WorkflowDefinitionDetail> {
  return useQuery({
    queryKey: workflowKeys.definition(name ?? ""),
    queryFn: () => client.getWorkflowDefinition(name as string),
    enabled: Boolean(name),
    placeholderData: keepPreviousData,
  });
}

/** Author a new custom workflow; refreshes the definition list. */
export function useCreateWorkflowDefinition(
  client: ForgeApiClient = apiClient,
): UseMutationResult<WorkflowDefinitionDetail, Error, CreateWorkflowDefinition> {
  const queryClient = useQueryClient();
  return useMutation<WorkflowDefinitionDetail, Error, CreateWorkflowDefinition>({
    mutationFn: (body) => client.createWorkflowDefinition(body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: workflowKeys.all() });
    },
  });
}

/** Fork a bundled workflow into an editable copy; refreshes the list + detail. */
export function useForkBundledWorkflow(
  client: ForgeApiClient = apiClient,
): UseMutationResult<WorkflowDefinitionDetail, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<WorkflowDefinitionDetail, Error, string>({
    mutationFn: (name) => client.forkBundledWorkflow(name),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: workflowKeys.all() });
    },
  });
}

export interface SaveWorkflowDraftVariables {
  name: string;
  body: SaveWorkflowDraftRequest;
}

/** Save the working graph as the draft (re-validated server-side on save). */
export function useSaveWorkflowDraft(
  client: ForgeApiClient = apiClient,
): UseMutationResult<WorkflowRevisionDetail, Error, SaveWorkflowDraftVariables> {
  const queryClient = useQueryClient();
  return useMutation<WorkflowRevisionDetail, Error, SaveWorkflowDraftVariables>({
    mutationFn: ({ name, body }) => client.saveWorkflowDraft(name, body),
    onSettled: (_data, _error, variables) => {
      void queryClient.invalidateQueries({
        queryKey: workflowKeys.definition(variables.name),
      });
      void queryClient.invalidateQueries({
        queryKey: workflowKeys.definitions(),
      });
    },
  });
}

/** Re-run validation on the saved draft; returns every issue. */
export function useValidateWorkflowDraft(
  client: ForgeApiClient = apiClient,
): UseMutationResult<WorkflowValidationIssue[], Error, string> {
  return useMutation<WorkflowValidationIssue[], Error, string>({
    mutationFn: (name) => client.validateWorkflowDraft(name),
  });
}

/** Publish the draft as the new active revision; refreshes the list + detail. */
export function usePublishWorkflow(
  client: ForgeApiClient = apiClient,
): UseMutationResult<WorkflowRevisionDetail, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<WorkflowRevisionDetail, Error, string>({
    mutationFn: (name) => client.publishWorkflow(name),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: workflowKeys.all() });
    },
  });
}
