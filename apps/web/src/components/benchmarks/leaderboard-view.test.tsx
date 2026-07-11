import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { PublicBenchmark, PublicLeaderboard } from "@/lib/api/types";

import { LeaderboardView } from "./leaderboard-view";

const SWE_SUITE: PublicBenchmark = {
  slug: "swe-tasks",
  version: "1.0.0",
  title: "SWE Tasks",
  description: "General software engineering tasks.",
  task_count: 42,
  primary_metric: "composite",
  content_hash: `sha256:${"a".repeat(64)}`,
};

const AGENT_SUITE: PublicBenchmark = {
  slug: "agent-ops",
  version: "2.1.0",
  title: "Agent Ops",
  description: "Multi-step agent operations tasks.",
  task_count: 18,
  primary_metric: "composite",
  content_hash: `sha256:${"b".repeat(64)}`,
};

const SUITES = [SWE_SUITE, AGENT_SUITE];

const SWE_LEADERBOARD: PublicLeaderboard = {
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
      composite_score: 0.912,
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
    {
      rank: 2,
      model_label: "beta-model-1",
      agent_mode: "swarm",
      composite_score: 0.803,
      verified: false,
      forge_version: null,
      submitter_name: "Beta Research",
      submitter_org: null,
      per_category: [],
      submitted_at: "2026-06-02T00:00:00Z",
      submission_id: "sub-2",
    },
  ],
};

const AGENT_LEADERBOARD: PublicLeaderboard = {
  slug: "agent-ops",
  version: "2.1.0",
  title: "Agent Ops",
  primary_metric: "composite",
  content_hash: `sha256:${"b".repeat(64)}`,
  generated_at: "2026-07-02T00:00:00Z",
  entries: [],
};

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listPublicBenchmarks: vi.fn(() => Promise.resolve(SUITES)),
    getPublicLeaderboard: vi.fn((slug: string) =>
      Promise.resolve(slug === "agent-ops" ? AGENT_LEADERBOARD : SWE_LEADERBOARD),
    ),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  return render(<LeaderboardView client={client} />, { wrapper: Wrapper });
}

describe("LeaderboardView", () => {
  it("lists suites and auto-selects the first one's ranked leaderboard", async () => {
    const client = makeClient();
    renderView(client);

    expect(await screen.findByTestId("suite-swe-tasks-1.0.0")).toBeInTheDocument();
    expect(screen.getByTestId("suite-agent-ops-2.1.0")).toBeInTheDocument();
    expect(screen.getByText(/2\s*suites/i)).toBeInTheDocument();

    expect(
      await screen.findByRole("heading", { level: 2, name: /swe tasks/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(client.getPublicLeaderboard).toHaveBeenCalledWith(
        "swe-tasks",
        "1.0.0",
      ),
    );

    const rows = await screen.findAllByTestId("leaderboard-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Acme Labs");
    expect(rows[0]).toHaveTextContent("0.912");
    expect(rows[0]).toHaveTextContent("#1");
  });

  it("shows a verified badge for verified rows and a self-reported one otherwise", async () => {
    renderView(makeClient());
    await screen.findByRole("heading", { level: 2, name: /swe tasks/i });

    const rows = await screen.findAllByTestId("leaderboard-row");
    expect(rows[0].querySelector('[data-testid="verified-badge"]')).toBeTruthy();
    expect(
      rows[1].querySelector('[data-testid="unverified-badge"]'),
    ).toBeTruthy();
  });

  it("selects another suite and loads its leaderboard", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("suite-agent-ops-2.1.0");

    fireEvent.click(screen.getByTestId("suite-agent-ops-2.1.0"));

    expect(
      await screen.findByRole("heading", { level: 2, name: /agent ops/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(client.getPublicLeaderboard).toHaveBeenCalledWith(
        "agent-ops",
        "2.1.0",
      ),
    );
    expect(await screen.findByTestId("empty-leaderboard")).toBeInTheDocument();
  });

  it("shows the empty-suites state when nothing is published", async () => {
    const client = makeClient({
      listPublicBenchmarks: vi.fn(() => Promise.resolve([])),
    });
    renderView(client);
    expect(await screen.findByTestId("empty-suites")).toBeInTheDocument();
  });

  it("shows loading skeletons while suites and leaderboard are in flight", () => {
    const client = makeClient({
      listPublicBenchmarks: vi.fn(() => new Promise<PublicBenchmark[]>(() => {})),
    });
    renderView(client);
    expect(screen.getByTestId("suites-skeleton")).toBeInTheDocument();
  });

  it("shows a leaderboard loading skeleton once a suite is selected", async () => {
    const client = makeClient({
      getPublicLeaderboard: vi.fn(() => new Promise<PublicLeaderboard>(() => {})),
    });
    renderView(client);
    await screen.findByTestId("suite-swe-tasks-1.0.0");
    expect(await screen.findByTestId("leaderboard-skeleton")).toBeInTheDocument();
  });

  it("degrades gracefully when the suite list errors", async () => {
    const client = makeClient({
      listPublicBenchmarks: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(client);
    expect(await screen.findByTestId("suites-error")).toBeInTheDocument();
  });

  it("degrades gracefully when the leaderboard errors", async () => {
    const client = makeClient({
      getPublicLeaderboard: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(client);
    await screen.findByTestId("suite-swe-tasks-1.0.0");
    expect(await screen.findByTestId("leaderboard-error")).toBeInTheDocument();
  });

  it("retries the suite list on error", async () => {
    const listFn = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(SUITES);
    const client = makeClient({ listPublicBenchmarks: listFn });
    renderView(client);

    const error = await screen.findByTestId("suites-error");
    fireEvent.click(
      error.querySelector("button") as HTMLButtonElement,
    );

    expect(await screen.findByTestId("suite-swe-tasks-1.0.0")).toBeInTheDocument();
  });
});
