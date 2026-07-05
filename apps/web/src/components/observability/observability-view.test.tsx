import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { CostSummary, CostTimeseries } from "@/lib/api/types";

import { ObservabilityView } from "./observability-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const SUMMARY: CostSummary = {
  scope: "workspace",
  scope_id: "ws",
  total_cost_usd: "0.43",
  total_prompt_tokens: 1200,
  total_completion_tokens: 800,
  group_by: "provider",
  buckets: [
    { key: "openai", cost_usd: "0.30", prompt_tokens: 800, completion_tokens: 500 },
    { key: "anthropic", cost_usd: "0.13", prompt_tokens: 400, completion_tokens: 300 },
  ],
};

const TIMESERIES: CostTimeseries = {
  scope: "workspace",
  scope_id: "ws",
  bucket: "day",
  group_by: "provider",
  series: {
    openai: [
      ["2026-06-01T00:00:00Z", "0.10"],
      ["2026-06-02T00:00:00Z", "0.20"],
    ],
    anthropic: [
      ["2026-06-01T00:00:00Z", "0.05"],
      ["2026-06-02T00:00:00Z", "0.08"],
    ],
  },
};

const METRICS = `# TYPE forge_retrieval_requests_total counter
forge_retrieval_requests_total{hit="false"} 8
forge_retrieval_requests_total{hit="true"} 42
# TYPE forge_mcp_freshness_lag_seconds gauge
forge_mcp_freshness_lag_seconds{connection="github"} 120
forge_mcp_freshness_lag_seconds{connection="notion"} 3600
# TYPE forge_reranker_delta histogram
forge_reranker_delta_count 10
forge_reranker_delta_sum 2.5
# TYPE forge_retrieval_latency_seconds histogram
forge_retrieval_latency_seconds_count{stage="semantic"} 50
forge_retrieval_latency_seconds_sum{stage="semantic"} 1.5
forge_retrieval_latency_seconds_count{stage="total"} 50
forge_retrieval_latency_seconds_sum{stage="total"} 6
`;

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getCostSummary: vi.fn(() => Promise.resolve(SUMMARY)),
    getCostTimeseries: vi.fn(() => Promise.resolve(TIMESERIES)),
    getMetricsExposition: vi.fn(() => Promise.resolve(METRICS)),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(<ObservabilityView client={client} />, { wrapper: Wrapper });
}

describe("ObservabilityView", () => {
  it("renders the KPI row, cost breakdown and derived retrieval quality", async () => {
    renderView(makeClient());

    // Hero spend + tokens from the real cost summary.
    expect(await screen.findByTestId("kpi-spend")).toHaveTextContent("$0.43");
    expect(screen.getByTestId("kpi-tokens")).toHaveTextContent("2,000"); // 1,200 + 800

    // Breakdown bars, sorted by spend.
    const breakdown = screen.getByTestId("cost-breakdown");
    expect(breakdown).toHaveTextContent("Openai");
    expect(breakdown).toHaveTextContent("Anthropic");

    // Recall@k derived from the metrics scrape (42 / 50).
    await waitFor(() =>
      expect(screen.getByTestId("kpi-recall")).toHaveTextContent("84.0%"),
    );

    // Per-stage latency (mean) + freshness lag.
    expect(await screen.findByTestId("latency-stages")).toHaveTextContent("semantic");
    expect(screen.getByTestId("latency-stages")).toHaveTextContent("30 ms");
    const freshness = await screen.findByTestId("freshness-list");
    expect(freshness).toHaveTextContent("notion");
    expect(freshness).toHaveTextContent("github");
  });

  it("switches the breakdown dimension on the server", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("cost-breakdown");

    fireEvent.click(screen.getByRole("button", { name: "Phase" }));

    await waitFor(() =>
      expect(client.getCostSummary).toHaveBeenCalledWith(
        expect.objectContaining({ group_by: "phase" }),
      ),
    );
  });

  it("reveals the spend-over-time data table for accessibility", async () => {
    renderView(makeClient());
    await screen.findByTestId("cost-trend");

    fireEvent.click(screen.getByRole("button", { name: "Table" }));

    const table = await screen.findByRole("table");
    expect(table).toHaveTextContent("Openai");
    expect(table).toHaveTextContent("Anthropic");
  });

  it("exports the current breakdown and announces it", async () => {
    renderView(makeClient());
    await waitFor(() =>
      expect(screen.getByTestId("export-csv")).toBeEnabled(),
    );

    fireEvent.click(screen.getByTestId("export-csv"));

    await waitFor(() =>
      expect(screen.getByTestId("obs-status")).toHaveTextContent(
        "Exported 2 rows to CSV.",
      ),
    );
  });

  it("shows a loading skeleton while cost is in flight", () => {
    const client = makeClient({
      getCostSummary: vi.fn(() => new Promise<CostSummary>(() => {})),
    });
    renderView(client);
    expect(screen.getByTestId("dashboard-skeleton")).toBeInTheDocument();
  });

  it("degrades gracefully when the cost service errors", async () => {
    const client = makeClient({
      getCostSummary: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(client);
    expect(await screen.findByTestId("cost-error")).toBeInTheDocument();
  });

  it("shows an empty state and disables export when there is no spend", async () => {
    const empty: CostSummary = {
      ...SUMMARY,
      total_cost_usd: "0",
      total_prompt_tokens: 0,
      total_completion_tokens: 0,
      buckets: [],
    };
    const client = makeClient({
      getCostSummary: vi.fn(() => Promise.resolve(empty)),
    });
    renderView(client);

    expect(await screen.findByTestId("empty-breakdown")).toBeInTheDocument();
    expect(screen.getByTestId("export-csv")).toBeDisabled();
  });

  it("marks the metric tiles as off when observability is disabled", async () => {
    const client = makeClient({
      getMetricsExposition: vi.fn(() => Promise.resolve("")),
    });
    renderView(client);

    await waitFor(() =>
      expect(screen.getByTestId("kpi-recall")).toHaveTextContent(
        "observability metrics off",
      ),
    );
    expect(within(screen.getByTestId("kpi-recall")).getByText("—")).toBeInTheDocument();
    expect(await screen.findByTestId("latency-off")).toBeInTheDocument();
    expect(screen.getByTestId("freshness-off")).toBeInTheDocument();
  });

  it("surfaces a metrics error without breaking the cost dashboard", async () => {
    const client = makeClient({
      getMetricsExposition: vi.fn(() => Promise.reject(new Error("scrape down"))),
    });
    renderView(client);

    // Cost still renders.
    expect(await screen.findByTestId("kpi-spend")).toHaveTextContent("$0.43");
    // Metric panels degrade.
    await waitFor(() =>
      expect(screen.getByTestId("kpi-recall")).toHaveTextContent(
        "metrics unavailable",
      ),
    );
    expect(await screen.findByTestId("latency-error")).toBeInTheDocument();
  });
});
