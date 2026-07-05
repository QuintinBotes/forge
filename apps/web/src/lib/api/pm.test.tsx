import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { type ForgeApiClient } from "./client";
import {
  pmKeys,
  useCreatePmConnection,
  useDisconnectPmConnection,
  usePatchPmConnection,
  usePmConnection,
  usePmLinks,
  useTestPmConnection,
} from "./pm";
import type { PmConnection, PmConnectionDetail } from "./types";

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

function makeDetail(over: Partial<PmConnectionDetail> = {}): PmConnectionDetail {
  return {
    id: "conn-1",
    provider: "jira",
    name: "Platform",
    project_id: "proj-1",
    external_base_url: "https://acme.atlassian.net",
    external_project_key: "ENG",
    external_project_id: "10001",
    auth_type: "api_token",
    account_label: "bot@acme.com",
    granted_scopes: ["read:jira-work"],
    sync_direction: "bidirectional",
    conflict_policy: "newest_wins",
    status_map: { "In Progress": "started" },
    priority_map: {},
    field_map: {},
    status: "connected",
    last_health_at: "2026-07-05T11:00:00Z",
    last_full_sync_at: "2026-07-05T10:00:00Z",
    has_credential: true,
    has_webhook_secret: true,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-05T11:00:00Z",
    link_counts: { synced: 4, conflict: 1 },
    ...over,
  };
}

describe("usePmConnection", () => {
  it("is disabled until a connection id is provided", async () => {
    const client = {
      getPmConnection: vi.fn(() => Promise.resolve(makeDetail())),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePmConnection(null, client), {
      wrapper: makeWrapper(newClient()),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(client.getPmConnection).not.toHaveBeenCalled();
  });

  it("fetches the detail once an id is provided", async () => {
    const client = {
      getPmConnection: vi.fn(() => Promise.resolve(makeDetail())),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePmConnection("conn-1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.getPmConnection).toHaveBeenCalledWith("conn-1");
    expect(result.current.data?.link_counts).toEqual({ synced: 4, conflict: 1 });
  });
});

describe("usePmLinks", () => {
  it("passes the state filter through to the client", async () => {
    const client = {
      listPmLinks: vi.fn(() => Promise.resolve([])),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => usePmLinks("conn-1", "conflict", client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.listPmLinks).toHaveBeenCalledWith("conn-1", "conflict");
  });
});

describe("usePatchPmConnection", () => {
  it("optimistically updates the detail cache and invalidates on settle", async () => {
    const queryClient = newClient();
    queryClient.setQueryData(pmKeys.connection("conn-1"), makeDetail());
    const client = {
      patchPmConnection: vi.fn(() =>
        Promise.resolve(makeDetail({ conflict_policy: "manual" }) as PmConnection),
      ),
    } as unknown as ForgeApiClient;
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => usePatchPmConnection(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      connectionId: "conn-1",
      body: { conflict_policy: "manual" },
    });

    // Optimistic write lands before the request resolves.
    await waitFor(() =>
      expect(
        queryClient.getQueryData<PmConnectionDetail>(pmKeys.connection("conn-1"))
          ?.conflict_policy,
      ).toBe("manual"),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: pmKeys.connection("conn-1"),
    });
  });

  it("rolls the cache back when the patch fails", async () => {
    const queryClient = newClient();
    queryClient.setQueryData(pmKeys.connection("conn-1"), makeDetail());
    const client = {
      patchPmConnection: vi.fn(() => Promise.reject(new Error("nope"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePatchPmConnection(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      connectionId: "conn-1",
      body: { conflict_policy: "manual" },
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(
      queryClient.getQueryData<PmConnectionDetail>(pmKeys.connection("conn-1"))
        ?.conflict_policy,
    ).toBe("newest_wins");
  });
});

describe("useCreatePmConnection", () => {
  it("invalidates the connection list after connecting", async () => {
    const queryClient = newClient();
    const client = {
      createPmConnection: vi.fn(() => Promise.resolve(makeDetail() as PmConnection)),
    } as unknown as ForgeApiClient;
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useCreatePmConnection(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      provider: "jira",
      name: "Platform",
      project_id: "proj-1",
      external_project_key: "ENG",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: pmKeys.connections() });
  });
});

describe("useDisconnectPmConnection / useTestPmConnection", () => {
  it("disconnect revalidates the list", async () => {
    const queryClient = newClient();
    const client = {
      disconnectPmConnection: vi.fn(() =>
        Promise.resolve(makeDetail({ status: "disabled" }) as PmConnection),
      ),
    } as unknown as ForgeApiClient;
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useDisconnectPmConnection(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate("conn-1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: pmKeys.connections() });
  });

  it("test returns the health verdict", async () => {
    const client = {
      testPmConnection: vi.fn(() =>
        Promise.resolve({
          status: "connected" as const,
          provider: "jira" as const,
          latency_ms: 42,
          account: "bot@acme.com",
          granted_scopes: ["read:jira-work"],
          error: null,
        }),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useTestPmConnection(client), {
      wrapper: makeWrapper(newClient()),
    });

    result.current.mutate("conn-1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.latency_ms).toBe(42);
  });
});
