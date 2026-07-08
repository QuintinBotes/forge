/**
 * Pure helpers for the Observability & cost screen (F38).
 *
 * Two concerns, no React, so they unit-test in isolation:
 *
 *  1. Parse the Prometheus text exposition served by `GET /observability/metrics`
 *     (the in-process F38 registry) and derive the retrieval-quality + latency
 *     signals the dashboard shows. The scrape renders histograms as `_count` +
 *     `_sum` only, so what we can honestly derive from it is a **mean** (sum ÷
 *     count) and **throughput** (count) per stage — not p50/p95/p99, which need
 *     the bucket/quantile exposition that is a documented backend follow-up.
 *  2. Formatting + number coercion shared by every tile and chart (cost amounts
 *     arrive as Decimal-strings over JSON, so everything coerces defensively).
 */

import type { CostBucket, CostSummary, CostTimeseries } from "@/lib/api/types";

// --- Number coercion + formatting ----------------------------------------- //

/** Coerce a JSON value that may be a Decimal-string (or null) to a number. */
export function toNum(value: number | string | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** Compact integer: 1,284 · 12.9K · 3.4M · 1.2B. */
export function compactNumber(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${trim(n / 1e9)}B`;
  if (abs >= 1e6) return `${trim(n / 1e6)}M`;
  if (abs >= 1e4) return `${trim(n / 1e3)}K`;
  return Math.round(n).toLocaleString("en-US");
}

function trim(n: number): string {
  // One decimal, but drop a trailing ".0" (12.0K -> 12K).
  return n.toFixed(1).replace(/\.0$/, "");
}

/** USD: precise 2dp under $10k, compact above ($1.2K / $3.4M). */
export function formatUsd(value: number | string | null | undefined): string {
  const n = toNum(value);
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1e4) return `${sign}$${compactNumber(abs)}`;
  return `${sign}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Token counts, always compact. */
export function formatTokens(value: number | string | null | undefined): string {
  return compactNumber(toNum(value));
}

/** Seconds -> human latency: "42 ms" / "1.20 s". */
export function formatLatency(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "—";
  }
  const ms = seconds * 1000;
  if (ms < 1000) return `${ms >= 100 ? Math.round(ms) : Number(ms.toFixed(1))} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Seconds -> a coarse "age": "42 s" / "12 min" / "3.1 h". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "—";
  }
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const mins = seconds / 60;
  if (mins < 90) return `${Math.round(mins)} min`;
  return `${(mins / 60).toFixed(1)} h`;
}

/** Fraction in [0,1] -> "94.2%" (null -> "—"). */
export function formatPct(fraction: number | null | undefined): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
    return "—";
  }
  return `${(fraction * 100).toFixed(1)}%`;
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** ISO timestamp -> deterministic short label ("Jun 3"), locale-independent. */
export function formatBucketLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

// --- Prometheus text parsing ---------------------------------------------- //

export interface PromSample {
  name: string;
  labels: Record<string, string>;
  value: number;
}

const SAMPLE_RE = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-\d.eE+]+)$/;

/** Parse the Prometheus text exposition into a flat list of samples. */
export function parsePrometheus(text: string): PromSample[] {
  const out: PromSample[] = [];
  if (!text) return out;
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = SAMPLE_RE.exec(line);
    if (!match) continue;
    const [, name, labelBlock, rawValue] = match;
    const value = Number(rawValue);
    if (!Number.isFinite(value)) continue;
    out.push({ name, labels: parseLabels(labelBlock), value });
  }
  return out;
}

function parseLabels(block: string | undefined): Record<string, string> {
  const labels: Record<string, string> = {};
  if (!block) return labels;
  const inner = block.slice(1, -1); // drop { }
  if (!inner) return labels;
  const pairRe = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g;
  let m: RegExpExecArray | null;
  while ((m = pairRe.exec(inner)) !== null) {
    labels[m[1]] = m[2].replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
  return labels;
}

function matches(sample: PromSample, want: Record<string, string>): boolean {
  return Object.entries(want).every(([k, v]) => sample.labels[k] === v);
}

/** Sum every sample of `name` whose labels superset-match `where`. */
function sumWhere(
  samples: PromSample[],
  name: string,
  where: Record<string, string> = {},
): number {
  return samples
    .filter((s) => s.name === name && matches(s, where))
    .reduce((acc, s) => acc + s.value, 0);
}

// --- Retrieval-quality + latency derivations ------------------------------ //

/** Retrieval pipeline stages, in pipeline order (`total` is the aggregate). */
export const RETRIEVAL_STAGES = [
  "semantic",
  "keyword",
  "fusion",
  "rerank",
  "total",
] as const;

export interface StageLatency {
  stage: string;
  meanSeconds: number;
  count: number;
}

export interface FreshnessEntry {
  connection: string;
  seconds: number;
}

export interface RetrievalQuality {
  /** True when the scrape carried no F38 series (OBS_ENABLED=false / empty). */
  empty: boolean;
  /** Recall@k proxy: hits ÷ (hits + misses). Null when no requests recorded. */
  hitRate: number | null;
  hitCount: number;
  missCount: number;
  /** Mean reranker score uplift (Δ). Null when no rerank happened. */
  rerankerDeltaMean: number | null;
  rerankerSamples: number;
  /** Per-stage mean latency + throughput (mean over the scrape window). */
  stages: StageLatency[];
  /** Freshness lag per MCP connection (gauge, seconds). */
  freshness: FreshnessEntry[];
  maxFreshnessSeconds: number | null;
}

/**
 * Fold a parsed exposition into the retrieval-quality view model. Every field
 * is derived from real series; a metric the scrape did not carry surfaces as
 * `null`/empty rather than a fabricated value.
 */
export function deriveRetrievalQuality(samples: PromSample[]): RetrievalQuality {
  const hitCount = sumWhere(samples, "forge_retrieval_requests_total", {
    hit: "true",
  });
  const missCount = sumWhere(samples, "forge_retrieval_requests_total", {
    hit: "false",
  });
  const requests = hitCount + missCount;

  const rerankerSamples = sumWhere(samples, "forge_reranker_delta_count");
  const rerankerSum = sumWhere(samples, "forge_reranker_delta_sum");

  const stages: StageLatency[] = [];
  const seen = new Set<string>();
  const pushStage = (stage: string) => {
    if (seen.has(stage)) return;
    const count = sumWhere(samples, "forge_retrieval_latency_seconds_count", {
      stage,
    });
    const sum = sumWhere(samples, "forge_retrieval_latency_seconds_sum", {
      stage,
    });
    if (count > 0) {
      stages.push({ stage, meanSeconds: sum / count, count });
      seen.add(stage);
    }
  };
  for (const stage of RETRIEVAL_STAGES) pushStage(stage);
  // Any bounded-to-"other" stage the catalog folded in.
  pushStage("other");

  const freshness: FreshnessEntry[] = samples
    .filter((s) => s.name === "forge_mcp_freshness_lag_seconds")
    .map((s) => ({ connection: s.labels.connection ?? "unknown", seconds: s.value }))
    .sort((a, b) => b.seconds - a.seconds);

  const empty =
    requests === 0 &&
    stages.length === 0 &&
    freshness.length === 0 &&
    rerankerSamples === 0;

  return {
    empty,
    hitRate: requests > 0 ? hitCount / requests : null,
    hitCount,
    missCount,
    rerankerDeltaMean: rerankerSamples > 0 ? rerankerSum / rerankerSamples : null,
    rerankerSamples,
    stages,
    freshness,
    maxFreshnessSeconds: freshness.length > 0 ? freshness[0].seconds : null,
  };
}

/** Parse + derive in one step (the query `select`). */
export function parseRetrievalQuality(text: string): RetrievalQuality {
  return deriveRetrievalQuality(parsePrometheus(text));
}

// --- Cost breakdown / trend shaping --------------------------------------- //

export interface BreakdownRow {
  key: string;
  label: string;
  costUsd: number;
  tokens: number;
  requestCount: number;
}

/** Human labels for the phase/provider group keys (fallback: title-case). */
export function prettyKey(key: string): string {
  if (!key) return "Unknown";
  return key
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

/** Cost buckets -> sorted (desc by spend) breakdown rows. */
export function toBreakdownRows(summary: CostSummary | undefined): BreakdownRow[] {
  if (!summary) return [];
  return summary.buckets
    .map((b: CostBucket) => ({
      key: b.key,
      label: prettyKey(b.key),
      costUsd: toNum(b.cost_usd),
      tokens: toNum(b.prompt_tokens) + toNum(b.completion_tokens),
      requestCount: toNum(b.request_count),
    }))
    .sort((a, b) => b.costUsd - a.costUsd);
}

export interface TrendPoint {
  t: string;
  value: number;
}

export interface TrendSeries {
  key: string;
  label: string;
  /** Slot index 0..5 into the categorical ramp; -1 for the folded "Other". */
  slot: number;
  points: TrendPoint[];
  total: number;
}

/**
 * Shape a {@link CostTimeseries} into ≤ `maxSeries` ranked series (by total
 * spend) plus a folded "Other" bucket, aligned onto the union of buckets so
 * every series shares one x-domain. Colour follows series identity (rank at
 * shaping time), never repainted as the set changes.
 */
export function toTrendSeries(
  ts: CostTimeseries | undefined,
  maxSeries = 6,
): { series: TrendSeries[]; buckets: string[] } {
  if (!ts || !ts.series) return { series: [], buckets: [] };

  const bucketSet = new Set<string>();
  const totals = new Map<string, number>();
  const byKey = new Map<string, Map<string, number>>();

  for (const [key, points] of Object.entries(ts.series)) {
    const perT = new Map<string, number>();
    let total = 0;
    for (const [t, cost] of points) {
      const v = toNum(cost);
      bucketSet.add(t);
      perT.set(t, (perT.get(t) ?? 0) + v);
      total += v;
    }
    byKey.set(key, perT);
    totals.set(key, total);
  }

  const buckets = Array.from(bucketSet).sort(
    (a, b) => new Date(a).getTime() - new Date(b).getTime(),
  );
  const ranked = Array.from(totals.entries()).sort((a, b) => b[1] - a[1]);
  const top = ranked.slice(0, maxSeries);
  const rest = ranked.slice(maxSeries);

  const series: TrendSeries[] = top.map(([key, total], i) => ({
    key,
    label: prettyKey(key),
    slot: i,
    total,
    points: buckets.map((t) => ({ t, value: byKey.get(key)?.get(t) ?? 0 })),
  }));

  if (rest.length > 0) {
    const otherTotal = rest.reduce((acc, [, v]) => acc + v, 0);
    series.push({
      key: "__other__",
      label: `Other (${rest.length})`,
      slot: -1,
      total: otherTotal,
      points: buckets.map((t) => ({
        t,
        value: rest.reduce((acc, [key]) => acc + (byKey.get(key)?.get(t) ?? 0), 0),
      })),
    });
  }

  return { series, buckets };
}
