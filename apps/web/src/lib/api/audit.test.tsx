import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import { useAuditLog, useAuditVocabulary, useVerifyAuditChain } from "./audit";
import type { AuditEntry, AuditListResponse } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function entry(id: string, over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id,
    workspace_id: "ws-1",
    seq: 1,
    action: "agent.action",
    actor_type: "user",
    result: "success",
    severity: "info",
    details: {},
    created_at: "2026-07-05T11:00:00Z",
    ...over,
  };
}

describe("useAuditLog", () => {
  it("flattens cursor-paginated pages and walks next_cursor", async () => {
    const page1: AuditListResponse = { items: [entry("a1")], next_cursor: "c2" };
    const page2: AuditListResponse = { items: [entry("a2")], next_cursor: null };
    const listAudit = vi.fn((query?: { cursor?: string }) =>
      Promise.resolve(query?.cursor === "c2" ? page2 : page1),
    );
    const client = { listAudit } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useAuditLog({ limit: 50 }, client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.pages[0].items).toHaveLength(1);
    expect(result.current.hasNextPage).toBe(true);

    // First call has no cursor.
    expect(listAudit).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 50, cursor: undefined }),
    );

    await result.current.fetchNextPage();

    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    expect(result.current.hasNextPage).toBe(false);
    expect(listAudit).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: "c2" }),
    );
  });
});

describe("useAuditVocabulary", () => {
  it("loads the filter vocabulary", async () => {
    const vocab = {
      actions: ["agent.action", "policy.tool_denied"],
      actor_types: ["user", "agent_runner"],
      resource_types: ["task"],
      outcomes: ["success", "denied"],
      severities: ["info", "critical"],
    };
    const client = {
      getAuditVocabulary: vi.fn(() => Promise.resolve(vocab)),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useAuditVocabulary(client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.actions).toContain("policy.tool_denied");
  });
});

describe("useVerifyAuditChain", () => {
  it("returns the chain verdict from the mutation", async () => {
    const verdict = {
      workspace_id: "ws-1",
      ok: true,
      entries_checked: 12,
      broken_at_seq: null,
      detail: null,
    };
    const client = {
      verifyAuditChain: vi.fn(() => Promise.resolve(verdict)),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useVerifyAuditChain(client), {
      wrapper: makeWrapper(newClient()),
    });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.entries_checked).toBe(12);
  });
});
