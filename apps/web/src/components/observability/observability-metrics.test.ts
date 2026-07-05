import { describe, expect, it } from "vitest";

import type { CostSummary, CostTimeseries } from "@/lib/api/types";

import {
  compactNumber,
  deriveRetrievalQuality,
  formatBucketLabel,
  formatDuration,
  formatLatency,
  formatPct,
  formatTokens,
  formatUsd,
  parsePrometheus,
  parseRetrievalQuality,
  prettyKey,
  toBreakdownRows,
  toNum,
  toTrendSeries,
} from "./observability-metrics";

// A slice of the exact `render_prometheus` exposition shape (count/sum for
// histograms, bare series for counters/gauges).
const EXPOSITION = `# TYPE forge_retrieval_requests_total counter
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

describe("parsePrometheus", () => {
  it("parses names, labels and values, skipping # comment lines", () => {
    const samples = parsePrometheus(EXPOSITION);
    const hitTrue = samples.find(
      (s) => s.name === "forge_retrieval_requests_total" && s.labels.hit === "true",
    );
    expect(hitTrue?.value).toBe(42);
    const semanticSum = samples.find(
      (s) =>
        s.name === "forge_retrieval_latency_seconds_sum" &&
        s.labels.stage === "semantic",
    );
    expect(semanticSum?.value).toBe(1.5);
    // No comment/type lines leak through as samples.
    expect(samples.every((s) => !s.name.startsWith("#"))).toBe(true);
  });

  it("handles an empty body", () => {
    expect(parsePrometheus("")).toEqual([]);
  });
});

describe("deriveRetrievalQuality", () => {
  it("derives hit rate, reranker mean, per-stage latency and freshness", () => {
    const rq = deriveRetrievalQuality(parsePrometheus(EXPOSITION));
    expect(rq.empty).toBe(false);
    expect(rq.hitCount).toBe(42);
    expect(rq.missCount).toBe(8);
    expect(rq.hitRate).toBeCloseTo(0.84, 5);
    expect(rq.rerankerDeltaMean).toBeCloseTo(0.25, 5);
    expect(rq.rerankerSamples).toBe(10);

    // Stages come back in pipeline order (semantic before the total aggregate).
    expect(rq.stages.map((s) => s.stage)).toEqual(["semantic", "total"]);
    expect(rq.stages[0].meanSeconds).toBeCloseTo(1.5 / 50, 6);
    expect(rq.stages[1].meanSeconds).toBeCloseTo(6 / 50, 6);

    // Freshness sorted worst-first, with the max surfaced.
    expect(rq.freshness.map((f) => f.connection)).toEqual(["notion", "github"]);
    expect(rq.maxFreshnessSeconds).toBe(3600);
  });

  it("reports empty for a disabled/no-op registry", () => {
    const rq = parseRetrievalQuality("");
    expect(rq.empty).toBe(true);
    expect(rq.hitRate).toBeNull();
    expect(rq.rerankerDeltaMean).toBeNull();
    expect(rq.stages).toEqual([]);
    expect(rq.freshness).toEqual([]);
  });
});

describe("formatters", () => {
  it("coerces Decimal-strings and nullish to numbers", () => {
    expect(toNum("0.43")).toBe(0.43);
    expect(toNum(12)).toBe(12);
    expect(toNum(null)).toBe(0);
    expect(toNum("not-a-number")).toBe(0);
  });

  it("formats USD precisely when small, compact when large", () => {
    expect(formatUsd("0.43")).toBe("$0.43");
    expect(formatUsd(1234.5)).toBe("$1,234.50");
    expect(formatUsd(12345)).toBe("$12.3K");
    expect(formatUsd(1_000_000)).toBe("$1M");
  });

  it("compacts big numbers and tokens", () => {
    expect(compactNumber(1284)).toBe("1,284");
    expect(compactNumber(12900)).toBe("12.9K");
    expect(compactNumber(3_400_000)).toBe("3.4M");
    expect(formatTokens(1_500_000)).toBe("1.5M");
  });

  it("formats latency, duration and percent", () => {
    expect(formatLatency(0.03)).toBe("30 ms");
    expect(formatLatency(1.2)).toBe("1.20 s");
    expect(formatLatency(null)).toBe("—");
    expect(formatDuration(30)).toBe("30 s");
    expect(formatDuration(120)).toBe("2 min");
    expect(formatDuration(3600)).toBe("60 min");
    expect(formatPct(0.84)).toBe("84.0%");
    expect(formatPct(null)).toBe("—");
  });

  it("labels buckets deterministically (UTC)", () => {
    expect(formatBucketLabel("2026-06-03T00:00:00Z")).toBe("Jun 3");
    expect(prettyKey("spec_drafting")).toBe("Spec Drafting");
  });
});

describe("cost shaping", () => {
  const summary: CostSummary = {
    scope: "workspace",
    scope_id: "ws",
    total_cost_usd: "0.43",
    total_prompt_tokens: 1200,
    total_completion_tokens: 800,
    group_by: "provider",
    buckets: [
      { key: "anthropic", cost_usd: "0.13", prompt_tokens: 400, completion_tokens: 300 },
      { key: "openai", cost_usd: "0.30", prompt_tokens: 800, completion_tokens: 500 },
    ],
  };

  it("sorts breakdown rows by spend and coerces amounts", () => {
    const rows = toBreakdownRows(summary);
    expect(rows.map((r) => r.key)).toEqual(["openai", "anthropic"]);
    expect(rows[0].costUsd).toBeCloseTo(0.3, 5);
    expect(rows[0].tokens).toBe(1300);
  });

  it("aligns timeseries onto a shared, sorted x-domain", () => {
    const ts: CostTimeseries = {
      scope: "workspace",
      scope_id: "ws",
      bucket: "day",
      group_by: "provider",
      series: {
        openai: [
          ["2026-06-02T00:00:00Z", "0.20"],
          ["2026-06-01T00:00:00Z", "0.10"],
        ],
        anthropic: [["2026-06-01T00:00:00Z", "0.05"]],
      },
    };
    const { series, buckets } = toTrendSeries(ts);
    expect(buckets).toEqual([
      "2026-06-01T00:00:00Z",
      "2026-06-02T00:00:00Z",
    ]);
    // Ranked by total spend: openai (0.30) before anthropic (0.05).
    expect(series.map((s) => s.key)).toEqual(["openai", "anthropic"]);
    expect(series[0].slot).toBe(0);
    // Missing bucket for anthropic fills as 0, aligned to the shared domain.
    expect(series[1].points).toEqual([
      { t: "2026-06-01T00:00:00Z", value: 0.05 },
      { t: "2026-06-02T00:00:00Z", value: 0 },
    ]);
  });

  it("folds series beyond the cap into a single Other bucket", () => {
    const many: CostTimeseries = {
      scope: "workspace",
      scope_id: "ws",
      bucket: "day",
      group_by: "model",
      series: Object.fromEntries(
        Array.from({ length: 8 }, (_, i) => [
          `m${i}`,
          [["2026-06-01T00:00:00Z", String(8 - i)]] as [string, string][],
        ]),
      ),
    };
    const { series } = toTrendSeries(many, 6);
    expect(series).toHaveLength(7); // 6 ranked + Other
    const other = series[series.length - 1];
    expect(other.key).toBe("__other__");
    expect(other.slot).toBe(-1);
    // Folds the two smallest (m6=2, m7=1).
    expect(other.total).toBeCloseTo(3, 5);
  });
});
