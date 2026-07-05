"use client";

import {
  Clock,
  Coins,
  Download,
  Gauge,
  LineChart,
  Sparkles,
  Target,
  Timer,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useCostSummary,
  useCostTimeseries,
  useObservabilityMetrics,
} from "@/lib/api/observability";
import type { CostGroupBy } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  BarBreakdown,
  Meter,
  StageLatencyBars,
  StatTile,
  TrendChart,
  type MeterTone,
} from "./charts";
import {
  compactNumber,
  formatDuration,
  formatLatency,
  formatPct,
  formatTokens,
  formatUsd,
  toBreakdownRows,
  toNum,
  toTrendSeries,
} from "./observability-metrics";

const GROUP_OPTIONS: { id: CostGroupBy; label: string }[] = [
  { id: "provider", label: "Provider" },
  { id: "phase", label: "Phase" },
  { id: "model", label: "Model" },
];

const WINDOW_OPTIONS: { id: number; label: string }[] = [
  { id: 7, label: "7d" },
  { id: 30, label: "30d" },
  { id: 90, label: "90d" },
];

export interface ObservabilityViewProps {
  client?: ForgeApiClient;
}

/**
 * Observability & cost (F38). One workspace-scoped dashboard: token/cost per
 * phase / provider / model (real `/cost/*`), spend over time, and the retrieval
 * quality + latency signals derived from the live `/observability/metrics`
 * scrape. Keyboard-first — the window and dimension are segmented controls, the
 * command palette can switch dimension or export, and the single ember action
 * exports the current breakdown.
 */
export function ObservabilityView({ client = apiClient }: ObservabilityViewProps) {
  const [groupBy, setGroupBy] = useState<CostGroupBy>("provider");
  const [windowDays, setWindowDays] = useState(30);
  const [status, setStatus] = useState<string | null>(null);

  const from = useMemo(
    () => new Date(Date.now() - windowDays * 86_400_000).toISOString(),
    [windowDays],
  );
  const bucket = windowDays > 45 ? "week" : "day";

  const summaryQuery = useCostSummary(
    { scope: "workspace", group_by: groupBy, from },
    client,
  );
  const trendQuery = useCostTimeseries(
    { scope: "workspace", group_by: groupBy, bucket, from },
    client,
  );
  const metricsQuery = useObservabilityMetrics(client);

  const summary = summaryQuery.data;
  const rows = useMemo(() => toBreakdownRows(summary), [summary]);
  const { series, buckets } = useMemo(
    () => toTrendSeries(trendQuery.data, 6),
    [trendQuery.data],
  );

  const totalCost = toNum(summary?.total_cost_usd);
  const promptTokens = toNum(summary?.total_prompt_tokens);
  const completionTokens = toNum(summary?.total_completion_tokens);
  const totalTokens = promptTokens + completionTokens;

  const spark = useMemo(
    () =>
      buckets.length > 1
        ? buckets.map((_, i) =>
            series.reduce((acc, s) => acc + (s.points[i]?.value ?? 0), 0),
          )
        : undefined,
    [buckets, series],
  );

  const rq = metricsQuery.data;

  const exportCsv = useCallback(() => {
    const buckets_ = summary?.buckets ?? [];
    const header = [
      "dimension",
      "key",
      "cost_usd",
      "prompt_tokens",
      "completion_tokens",
    ];
    const lines = [header.join(",")];
    for (const b of buckets_) {
      lines.push(
        [groupBy, b.key, toNum(b.cost_usd), b.prompt_tokens, b.completion_tokens].join(
          ",",
        ),
      );
    }
    const csv = lines.join("\n");
    try {
      const a = document.createElement("a");
      a.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`;
      a.download = `forge-cost-${groupBy}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch {
      // Download is best-effort; the status line is the confirmation.
    }
    const count = buckets_.length;
    setStatus(`Exported ${count} ${count === 1 ? "row" : "rows"} to CSV.`);
  }, [summary, groupBy]);

  // --- command palette (stable refs -> latest handlers) --- //
  const exportRef = useRef(exportCsv);
  const setGroupByRef = useRef(setGroupBy);
  useEffect(() => {
    exportRef.current = exportCsv;
  }, [exportCsv]);
  const commands = useMemo(
    () => [
      {
        id: "obs-export",
        label: "Export cost report (CSV)",
        group: "Observability",
        icon: <Download />,
        run: () => exportRef.current(),
      },
      ...GROUP_OPTIONS.map((opt) => ({
        id: `obs-group-${opt.id}`,
        label: `Group cost by ${opt.label.toLowerCase()}`,
        group: "Observability",
        icon: <Coins />,
        run: () => setGroupByRef.current(opt.id),
      })),
    ],
    [],
  );
  useRegisterCommands("observability", commands);

  const dimensionLabel =
    GROUP_OPTIONS.find((o) => o.id === groupBy)?.label ?? "Provider";
  const hasRows = rows.length > 0;

  return (
    <div
      data-testid="observability"
      role="region"
      aria-label="Observability and cost"
      className="flex h-full flex-col gap-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <Gauge className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight">
              Observability &amp; cost
            </h1>
            <p className="text-sm text-muted-foreground">
              Token spend, retrieval quality and latency across the workspace.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Segmented
            ariaLabel="Time window"
            options={WINDOW_OPTIONS.map((w) => ({ id: String(w.id), label: w.label }))}
            value={String(windowDays)}
            onChange={(v) => setWindowDays(Number(v))}
          />
          <Button
            onClick={exportCsv}
            disabled={!hasRows}
            data-testid="export-csv"
          >
            <Download className="h-4 w-4" aria-hidden />
            Export
          </Button>
        </div>
      </header>

      <span data-testid="obs-status" role="status" aria-live="polite" className="sr-only">
        {status}
      </span>

      {summaryQuery.isLoading ? (
        <DashboardSkeleton />
      ) : summaryQuery.isError ? (
        <CostError />
      ) : (
        <div className="flex flex-col gap-5">
          {/* KPI row */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              testId="kpi-spend"
              label={`Total spend · ${windowDays}d`}
              value={formatUsd(totalCost)}
              accent
              icon={<Coins className="h-3.5 w-3.5" aria-hidden />}
              spark={spark}
              sub={`${compactNumber(rows.length)} ${dimensionLabel.toLowerCase()}${rows.length === 1 ? "" : "s"} active`}
            />
            <StatTile
              testId="kpi-tokens"
              label="Tokens"
              value={formatTokens(totalTokens)}
              icon={<LineChart className="h-3.5 w-3.5" aria-hidden />}
              sub={
                <TokenSplit prompt={promptTokens} completion={completionTokens} />
              }
            />
            <MetricTile
              testId="kpi-recall"
              label="Recall@k (hit rate)"
              icon={<Target className="h-3.5 w-3.5" aria-hidden />}
              query={metricsQuery}
              value={rq ? formatPct(rq.hitRate) : "—"}
              sub={
                rq && rq.hitRate !== null
                  ? `${compactNumber(rq.hitCount)} hits · ${compactNumber(rq.hitCount + rq.missCount)} queries`
                  : "no retrievals in window"
              }
            />
            <MetricTile
              testId="kpi-reranker"
              label="Reranker uplift"
              icon={<Sparkles className="h-3.5 w-3.5" aria-hidden />}
              query={metricsQuery}
              value={
                rq && rq.rerankerDeltaMean !== null
                  ? `${rq.rerankerDeltaMean >= 0 ? "+" : ""}${rq.rerankerDeltaMean.toFixed(3)}`
                  : "—"
              }
              sub={
                rq && rq.rerankerDeltaMean !== null
                  ? `mean Δ · ${compactNumber(rq.rerankerSamples)} reranked`
                  : "no reranks in window"
              }
            />
          </div>

          {/* Cost breakdown + trend */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel
              title={`Cost by ${dimensionLabel.toLowerCase()}`}
              icon={<Coins className="h-4 w-4" aria-hidden />}
              action={
                <Segmented
                  ariaLabel="Breakdown dimension"
                  options={GROUP_OPTIONS.map((o) => ({ id: o.id, label: o.label }))}
                  value={groupBy}
                  onChange={(v) => setGroupBy(v as CostGroupBy)}
                  size="sm"
                />
              }
            >
              {hasRows ? (
                <BarBreakdown
                  testId="cost-breakdown"
                  ariaLabel={`Cost by ${dimensionLabel.toLowerCase()}`}
                  bars={rows.map((r) => ({
                    key: r.key,
                    label: r.label,
                    value: r.costUsd,
                    secondary: `${formatTokens(r.tokens)} tok`,
                  }))}
                  formatValue={(n) => formatUsd(n)}
                />
              ) : (
                <EmptyState
                  testId="empty-breakdown"
                  icon={<Coins className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No spend recorded yet"
                  body={`Once agents run priced model calls in the last ${windowDays} days, spend by ${dimensionLabel.toLowerCase()} lands here.`}
                />
              )}
            </Panel>

            <Panel
              title="Spend over time"
              icon={<LineChart className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">
                  per {bucket}
                </span>
              }
            >
              {trendQuery.isError ? (
                <PanelNote testId="trend-error">
                  Spend timeseries is unavailable right now.
                </PanelNote>
              ) : series.length > 0 && buckets.length > 0 ? (
                <TrendChart
                  testId="cost-trend"
                  series={series}
                  buckets={buckets}
                  formatValue={(n) => formatUsd(n)}
                />
              ) : (
                <EmptyState
                  testId="empty-trend"
                  icon={<LineChart className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No trend yet"
                  body="Spend needs at least one recorded bucket to chart over time."
                />
              )}
            </Panel>
          </div>

          {/* Retrieval quality + latency */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel
              title="Retrieval latency by stage"
              icon={<Timer className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">mean over window</span>
              }
            >
              <RetrievalLatency query={metricsQuery} />
            </Panel>

            <Panel
              title="Index freshness"
              icon={<Clock className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">lag per connection</span>
              }
            >
              <Freshness query={metricsQuery} />
            </Panel>
          </div>
        </div>
      )}
    </div>
  );
}

// --- Sub-panels ----------------------------------------------------------- //

type MetricsQuery = ReturnType<typeof useObservabilityMetrics>;

function RetrievalLatency({ query }: { query: MetricsQuery }) {
  if (query.isLoading) return <BarsSkeleton testId="latency-skeleton" rows={4} />;
  if (query.isError) {
    return <PanelNote testId="latency-error">Latency metrics are unavailable.</PanelNote>;
  }
  const rq = query.data;
  if (!rq || rq.stages.length === 0) {
    return (
      <MetricsOff testId="latency-off">
        No retrieval latency recorded in this window.
      </MetricsOff>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <StageLatencyBars
        testId="latency-stages"
        stages={rq.stages}
        formatValue={formatLatency}
      />
      <p className="text-[11px] leading-relaxed text-muted-foreground/80">
        Mean + throughput from the live scrape. True p50/p95/p99 percentiles need
        the histogram-bucket exposition (a documented F38 follow-up).
      </p>
    </div>
  );
}

function Freshness({ query }: { query: MetricsQuery }) {
  if (query.isLoading) return <BarsSkeleton testId="freshness-skeleton" rows={3} />;
  if (query.isError) {
    return <PanelNote testId="freshness-error">Freshness metrics are unavailable.</PanelNote>;
  }
  const rq = query.data;
  if (!rq || rq.freshness.length === 0) {
    return (
      <MetricsOff testId="freshness-off">
        No connected MCP indexes are reporting freshness lag.
      </MetricsOff>
    );
  }
  const max = Math.max(...rq.freshness.map((f) => f.seconds), 1);
  return (
    <ul data-testid="freshness-list" aria-label="Freshness lag by connection" className="flex flex-col gap-3">
      {rq.freshness.map((f) => (
        <li key={f.connection}>
          <Meter
            label={f.connection}
            valueText={formatDuration(f.seconds)}
            fraction={f.seconds / max}
            tone={freshnessTone(f.seconds)}
          />
        </li>
      ))}
    </ul>
  );
}

function freshnessTone(seconds: number): MeterTone {
  if (seconds < 300) return "success"; // < 5 min
  if (seconds < 1800) return "warning"; // < 30 min
  return "danger";
}

function TokenSplit({ prompt, completion }: { prompt: number; completion: number }) {
  const total = prompt + completion || 1;
  const promptPct = (prompt / total) * 100;
  return (
    <div className="flex flex-col gap-1">
      <div
        className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`Prompt ${formatTokens(prompt)} tokens, completion ${formatTokens(completion)} tokens`}
      >
        <span style={{ width: `${promptPct}%`, backgroundColor: "hsl(var(--chart-2))" }} />
        <span style={{ width: `${100 - promptPct}%`, backgroundColor: "hsl(var(--chart-4))" }} />
      </div>
      <span className="text-[11px] text-muted-foreground">
        {formatTokens(prompt)} prompt · {formatTokens(completion)} completion
      </span>
    </div>
  );
}

// --- Small building blocks ------------------------------------------------ //

function Panel({
  title,
  icon,
  action,
  children,
}: {
  title: string;
  icon?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex min-w-0 flex-col gap-4 rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
          <span className="text-muted-foreground">{icon}</span>
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function MetricTile({
  label,
  icon,
  value,
  sub,
  query,
  testId,
}: {
  label: string;
  icon: ReactNode;
  value: string;
  sub: ReactNode;
  query: MetricsQuery;
  testId?: string;
}) {
  if (query.isLoading) {
    return (
      <div
        data-testid={testId}
        aria-busy="true"
        className="flex h-[104px] flex-col gap-2 rounded-lg border border-border bg-card p-4"
      >
        <div className="h-3 w-24 animate-pulse rounded bg-muted" />
        <div className="mt-1 h-7 w-20 animate-pulse rounded bg-muted" />
        <div className="mt-auto h-3 w-28 animate-pulse rounded bg-muted/60" />
      </div>
    );
  }
  const off = query.isError || !query.data || query.data.empty;
  return (
    <StatTile
      testId={testId}
      label={label}
      icon={icon}
      value={value}
      sub={
        query.isError
          ? "metrics unavailable"
          : off
            ? "observability metrics off"
            : sub
      }
    />
  );
}

export interface SegmentedOption {
  id: string;
  label: string;
}

function Segmented({
  options,
  value,
  onChange,
  ariaLabel,
  size = "default",
}: {
  options: SegmentedOption[];
  value: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  size?: "default" | "sm";
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
    >
      {options.map((opt) => {
        const isActive = value === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            aria-pressed={isActive}
            onClick={() => onChange(opt.id)}
            className={cn(
              "rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm",
              isActive
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function EmptyState({
  icon,
  title,
  body,
  testId,
}: {
  icon: ReactNode;
  title: string;
  body: string;
  testId?: string;
}) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border px-6 py-10 text-center"
    >
      {icon}
      <p className="text-sm font-medium text-foreground">{title}</p>
      <p className="max-w-sm text-xs text-muted-foreground">{body}</p>
    </div>
  );
}

function PanelNote({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div
      role="status"
      data-testid={testId}
      className="rounded-md border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground"
    >
      {children}
    </div>
  );
}

function MetricsOff({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className="rounded-md border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground"
    >
      {children}
    </div>
  );
}

function BarsSkeleton({ rows, testId }: { rows: number; testId?: string }) {
  return (
    <div data-testid={testId} aria-busy="true" className="flex flex-col gap-3">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex flex-col gap-1.5">
          <div className="h-3 w-1/3 animate-pulse rounded bg-muted" />
          <div className="h-2.5 w-full animate-pulse rounded-full bg-muted/60" />
        </div>
      ))}
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div data-testid="dashboard-skeleton" aria-busy="true" className="flex flex-col gap-5">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div
            key={i}
            className="flex h-[104px] flex-col gap-2 rounded-lg border border-border bg-card p-4"
          >
            <div className="h-3 w-24 animate-pulse rounded bg-muted" />
            <div className="mt-1 h-7 w-20 animate-pulse rounded bg-muted" />
            <div className="mt-auto h-3 w-28 animate-pulse rounded bg-muted/60" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {Array.from({ length: 2 }, (_, i) => (
          <div key={i} className="h-64 animate-pulse rounded-lg border border-border bg-card" />
        ))}
      </div>
    </div>
  );
}

function CostError() {
  return (
    <div
      role="status"
      data-testid="cost-error"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <Gauge className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">Cost metrics unavailable</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        The cost service is unreachable — spend and token rollups will return
        once it is back.
      </p>
    </div>
  );
}
