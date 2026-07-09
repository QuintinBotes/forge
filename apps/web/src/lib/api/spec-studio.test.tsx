import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import { useDraftSpec } from "./spec-studio";
import type { SpecDraft } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useDraftSpec", () => {
  it("posts the goal (+ optional epic/project) to the client and returns the draft", async () => {
    const result: SpecDraft = {
      goal: "Search orders by name",
      model: "claude-opus-4-8",
      spec_md: "---\nid: SPEC-DRAFT\n---\n\n## Goal\n\nSearch orders by name\n",
      manifest: { id: "SPEC-DRAFT", name: "Search orders by name" },
      usage: { cost_usd: 0.01 },
    };
    const client = { draftSpec: vi.fn(() => Promise.resolve(result)) } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result: hook } = renderHook(() => useDraftSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    hook.current.mutate({ goal: "Search orders by name", epic_id: "e1", project_id: "p1" });

    await waitFor(() => expect(hook.current.isSuccess).toBe(true));
    expect(hook.current.data).toEqual(result);
    expect(client.draftSpec).toHaveBeenCalledWith({
      goal: "Search orders by name",
      epic_id: "e1",
      project_id: "p1",
    });
  });

  it("nothing is persisted or cached — a draft is never written to a query key", async () => {
    const client = {
      draftSpec: vi.fn(() =>
        Promise.resolve({
          goal: "g",
          model: "m",
          spec_md: "---\nid: SPEC-DRAFT\n---\n\n## Goal\n\ng\n",
        } as SpecDraft),
      ),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result: hook } = renderHook(() => useDraftSpec(client), {
      wrapper: makeWrapper(queryClient),
    });
    hook.current.mutate({ goal: "g" });
    await waitFor(() => expect(hook.current.isSuccess).toBe(true));

    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
