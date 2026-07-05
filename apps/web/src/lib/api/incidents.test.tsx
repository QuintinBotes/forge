import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "./client";
import {
  incidentKeys,
  usePostmortem,
  useRemediationPlan,
  useSendIncidentEvent,
} from "./incidents";
import type { IncidentDetailView } from "./types";

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

describe("useRemediationPlan", () => {
  it("resolves a 404 (no plan proposed) to null rather than erroring", async () => {
    const client = {
      getRemediationPlan: vi.fn(() =>
        Promise.reject(new ApiError(404, "no remediation plan", null)),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useRemediationPlan("i1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("propagates non-404 errors", async () => {
    const client = {
      getRemediationPlan: vi.fn(() =>
        Promise.reject(new ApiError(500, "boom", null)),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useRemediationPlan("i1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("usePostmortem", () => {
  it("resolves a 404 (not resolved yet) to null", async () => {
    const client = {
      getPostmortem: vi.fn(() =>
        Promise.reject(new ApiError(404, "no postmortem", null)),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePostmortem("i1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });
});

describe("useSendIncidentEvent", () => {
  it("seeds the detail cache with the returned view on success", async () => {
    const detail: IncidentDetailView = {
      id: "i1",
      key: "INC-1",
      project_id: "proj-1",
      title: "Latency spike",
      severity: "high",
      state: "impact_assessed",
      lifecycle_state: "impact_assessed",
      source: "manual",
      created_at: "2026-07-05T11:00:00Z",
      allowed_events: ["remediation_proposed"],
      event_count: 4,
    };
    const client = {
      sendIncidentEvent: vi.fn(() => Promise.resolve(detail)),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();

    const { result } = renderHook(() => useSendIncidentEvent(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({ incidentId: "i1", body: { event: "impact_assessed" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.sendIncidentEvent).toHaveBeenCalledWith("i1", {
      event: "impact_assessed",
    });
    expect(queryClient.getQueryData(incidentKeys.detail("i1"))).toEqual(detail);
  });
});
