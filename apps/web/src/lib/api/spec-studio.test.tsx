import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import { useDraftSpec, useImportSpec } from "./spec-studio";
import type { SpecDraft, SpecImport } from "./types";

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

describe("useImportSpec", () => {
  it("posts the content (+ optional source_format) to the client and returns the import", async () => {
    const result: SpecImport = {
      source_format: "markdown",
      spec_md: "---\nid: SPEC-IMPORT\n---\n\n## Goal\n\nImported feature\n",
      manifest: { id: "SPEC-IMPORT", name: "Imported feature" },
      normalized: true,
    };
    const client = {
      importSpec: vi.fn(() => Promise.resolve(result)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result: hook } = renderHook(() => useImportSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    hook.current.mutate({ content: "# Imported feature\n", source_format: "markdown" });

    await waitFor(() => expect(hook.current.isSuccess).toBe(true));
    expect(hook.current.data).toEqual(result);
    expect(client.importSpec).toHaveBeenCalledWith({
      content: "# Imported feature\n",
      source_format: "markdown",
    });
  });

  it("nothing is persisted or cached — an import is never written to a query key", async () => {
    const client = {
      importSpec: vi.fn(() =>
        Promise.resolve({
          source_format: "yaml",
          spec_md: "---\nid: SPEC-IMPORT\n---\n\n## Goal\n\ng\n",
          normalized: false,
        } as SpecImport),
      ),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result: hook } = renderHook(() => useImportSpec(client), {
      wrapper: makeWrapper(queryClient),
    });
    hook.current.mutate({ content: "id: x\nname: g\n" });
    await waitFor(() => expect(hook.current.isSuccess).toBe(true));

    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
