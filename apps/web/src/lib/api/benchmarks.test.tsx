import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import { benchmarkKeys, usePublicBenchmarks, usePublicLeaderboard } from "./benchmarks";
import type { PublicBenchmark, PublicLeaderboard } from "./types";

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

const SUITE: PublicBenchmark = {
  slug: "swe-tasks",
  version: "1.0.0",
  title: "SWE Tasks",
  description: "General software engineering tasks.",
  task_count: 42,
  primary_metric: "composite",
  content_hash: `sha256:${"a".repeat(64)}`,
};

const LEADERBOARD: PublicLeaderboard = {
  slug: "swe-tasks",
  version: "1.0.0",
  title: "SWE Tasks",
  primary_metric: "composite",
  content_hash: `sha256:${"a".repeat(64)}`,
  generated_at: "2026-07-01T00:00:00Z",
  entries: [
    {
      rank: 1,
      model_label: "acme-model-2",
      agent_mode: "single_agent",
      composite_score: 0.91,
      verified: true,
      forge_version: "1.4.0",
      submitter_name: "Acme Labs",
      submitter_org: "Acme",
      per_category: [
        { category: "coding", score: 0.9, weight: 1, case_count: 20 },
      ],
      submitted_at: "2026-06-01T00:00:00Z",
      submission_id: "sub-1",
    },
  ],
};

describe("usePublicBenchmarks", () => {
  it("lists the published suites", async () => {
    const client = {
      listPublicBenchmarks: vi.fn(() => Promise.resolve([SUITE])),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePublicBenchmarks(client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([SUITE]);
    expect(client.listPublicBenchmarks).toHaveBeenCalledTimes(1);
  });

  it("propagates errors (e.g. the leaderboard being disabled → 404)", async () => {
    const client = {
      listPublicBenchmarks: vi.fn(() => Promise.reject(new Error("disabled"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePublicBenchmarks(client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("usePublicLeaderboard", () => {
  it("is disabled until both slug and version are known", () => {
    const client = {
      getPublicLeaderboard: vi.fn(),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => usePublicLeaderboard(null, null, client), {
      wrapper: makeWrapper(newClient()),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(client.getPublicLeaderboard).not.toHaveBeenCalled();
  });

  it("fetches the ranked leaderboard once a suite is selected", async () => {
    const client = {
      getPublicLeaderboard: vi.fn(() => Promise.resolve(LEADERBOARD)),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => usePublicLeaderboard("swe-tasks", "1.0.0", client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(LEADERBOARD);
    expect(client.getPublicLeaderboard).toHaveBeenCalledWith("swe-tasks", "1.0.0");
  });
});

describe("benchmarkKeys", () => {
  it("scopes the leaderboard key by slug + version", () => {
    expect(benchmarkKeys.leaderboard("a", "1")).not.toEqual(
      benchmarkKeys.leaderboard("a", "2"),
    );
  });
});
