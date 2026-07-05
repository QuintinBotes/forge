import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { approvalKeys, useApprovals, useDecideApproval } from "./approvals";
import type { ForgeApiClient } from "./client";
import type { ApprovalResolution, ApprovalSummary } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

const gate: ApprovalSummary = {
  id: "a1",
  gate_type: "pr",
  status: "pending",
  title: "Merge auth refactor",
  risk_level: "warning",
};

describe("useApprovals", () => {
  it("fetches the pending queue via the client", async () => {
    const client = {
      listApprovals: vi.fn(() => Promise.resolve([gate])),
    } as unknown as ForgeApiClient;

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { result } = renderHook(() => useApprovals({ status: "pending" }, client), {
      wrapper: makeWrapper(queryClient),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([gate]);
    expect(client.listApprovals).toHaveBeenCalledWith({ status: "pending" });
  });
});

describe("useDecideApproval (optimistic)", () => {
  it("drops the decided gate from the cached queue before the request resolves", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(approvalKeys.list({ status: "pending" }), [gate]);

    let resolve!: (value: ApprovalResolution) => void;
    const pending = new Promise<ApprovalResolution>((r) => {
      resolve = r;
    });
    const client = {
      decideApproval: vi.fn(() => pending),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useDecideApproval(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ approvalId: "a1", body: { decision: "approve" } });
    });

    await waitFor(() => {
      const list = queryClient.getQueryData<ApprovalSummary[]>(
        approvalKeys.list({ status: "pending" }),
      );
      expect(list).toEqual([]);
    });

    act(() => {
      resolve({ approval_id: "a1", status: "approved", outcome: { completed: true } });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("rolls the queue back when the decision fails", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(approvalKeys.list({ status: "pending" }), [gate]);

    const client = {
      decideApproval: vi.fn(() => Promise.reject(new Error("boom"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useDecideApproval(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ approvalId: "a1", body: { decision: "reject", note: "no" } });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));

    const list = queryClient.getQueryData<ApprovalSummary[]>(
      approvalKeys.list({ status: "pending" }),
    );
    expect(list).toEqual([gate]);
  });
});
