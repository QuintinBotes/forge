"use client";

/**
 * TanStack Query hooks for the F36 human-approval inbox.
 *
 * Kept in a dedicated module (rather than the board `hooks.ts`) so the approval
 * surface owns its own query keys and cache-invalidation policy. The decision
 * mutation is **optimistic** (spec UX standard: "state changes appear instantly,
 * rollback on error") — it drops the resolved gate from every cached pending
 * list the moment the reviewer acts, then revalidates on settle so a gate that
 * still needs further approvals reappears.
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
  ApprovalContext,
  ApprovalCount,
  ApprovalDecisionRecord,
  ApprovalDecisionRequest,
  ApprovalResolution,
  ApprovalSummary,
  RedTeamGateOut,
} from "./types";

export const approvalKeys = {
  all: () => ["approvals"] as const,
  lists: () => ["approvals", "list"] as const,
  list: (filters?: Record<string, unknown>) =>
    ["approvals", "list", filters ?? {}] as const,
  count: (filters?: Record<string, unknown>) =>
    ["approvals", "count", filters ?? {}] as const,
  context: (approvalId: string) =>
    ["approvals", "context", approvalId] as const,
  decisions: (approvalId: string) =>
    ["approvals", "decisions", approvalId] as const,
  redTeam: (workflowRunId: string) =>
    ["approvals", "red-team", workflowRunId] as const,
} as const;

export type ApprovalFilters = Record<
  string,
  string | number | boolean | undefined
>;

/** The pending (or filtered) approval queue. */
export function useApprovals(
  filters?: ApprovalFilters,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ApprovalSummary[]> {
  return useQuery({
    queryKey: approvalKeys.list(filters),
    queryFn: () => client.listApprovals(filters),
  });
}

/** The pending-count nav badge. */
export function useApprovalCount(
  filters?: ApprovalFilters,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ApprovalCount> {
  return useQuery({
    queryKey: approvalKeys.count(filters),
    queryFn: () => client.approvalCount(filters),
  });
}

/** The nine "must-show" review items for one gate. */
export function useApprovalContext(
  approvalId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ApprovalContext> {
  return useQuery({
    queryKey: approvalKeys.context(approvalId ?? ""),
    queryFn: () => client.getApprovalContext(approvalId as string),
    enabled: Boolean(approvalId),
  });
}

/** The per-approver decision trail for one gate. */
export function useApprovalDecisions(
  approvalId: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ApprovalDecisionRecord[]> {
  return useQuery({
    queryKey: approvalKeys.decisions(approvalId ?? ""),
    queryFn: () => client.listApprovalDecisions(approvalId as string),
    enabled: Boolean(approvalId),
  });
}

/**
 * The Red-Team Gate verdict + evidence for the gate's linked workflow run
 * (Red-Team Gate, slice redteam-surface) — disabled until a run id is known
 * (a gate's `run_trace_ref.workflow_run_id`, from its context). Retries are
 * disabled: an unscanned run reads as `latest: null`, not an error, so there
 * is nothing a retry would resolve.
 */
export function useRedTeamVerdict(
  workflowRunId: string | null | undefined,
  client: ForgeApiClient = apiClient,
): UseQueryResult<RedTeamGateOut> {
  return useQuery({
    queryKey: approvalKeys.redTeam(workflowRunId ?? ""),
    queryFn: () => client.getWorkflowRunRedTeam(workflowRunId as string),
    enabled: Boolean(workflowRunId),
    retry: false,
  });
}

export interface DecideVariables {
  approvalId: string;
  body: ApprovalDecisionRequest;
}

interface DecideContext {
  previousLists: [readonly unknown[], ApprovalSummary[] | undefined][];
}

/**
 * Optimistic decision mutation.
 *
 * `onMutate` snapshots and drops the gate from every cached inbox list so the
 * queue updates instantly; `onError` restores the snapshots; `onSettled`
 * revalidates the lists + counts from the server.
 */
export function useDecideApproval(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ApprovalResolution, Error, DecideVariables, DecideContext> {
  const queryClient = useQueryClient();
  return useMutation<ApprovalResolution, Error, DecideVariables, DecideContext>({
    mutationFn: ({ approvalId, body }) => client.decideApproval(approvalId, body),
    onMutate: async ({ approvalId }) => {
      await queryClient.cancelQueries({ queryKey: approvalKeys.lists() });
      const previousLists = queryClient.getQueriesData<ApprovalSummary[]>({
        queryKey: approvalKeys.lists(),
      });
      queryClient.setQueriesData<ApprovalSummary[]>(
        { queryKey: approvalKeys.lists() },
        (old) => old?.filter((item) => item.id !== approvalId),
      );
      return { previousLists };
    },
    onError: (_error, _variables, context) => {
      if (!context) {
        return;
      }
      for (const [key, data] of context.previousLists) {
        queryClient.setQueryData(key, data);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: approvalKeys.all() });
    },
  });
}
