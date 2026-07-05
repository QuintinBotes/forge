"use client";

/**
 * Dependency-free chart primitives for the Observability & cost screen.
 *
 * Everything is inline HTML/SVG styled with Forge design tokens (no chart
 * library, no hardcoded colour) so it is theme-aware in light + dark and can
 * never reach for a CDN. Marks follow the dataviz specs: thin bars with a
 * rounded data-end, a 2px surface ring on overlapping dots, 2px lines, a legend
 * for ≥ 2 series, and a table view so identity is never colour-only.
 */

import { useId, useState, type CSSProperties, type ReactNode } from "react";

import { cn } from "@/lib/utils";

import type { StageLatency, TrendSeries } from "./observability-metrics";
import { compactNumber, formatBucketLabel } from "./observability-metrics";

/** Categorical ramp slot -> token. Slot -1 (folded "Other") reads muted. */
export function slotColor(slot: number): string {
  return slot < 0
    ? "hsl(var(--muted-foreground))"
    : `hsl(var(--chart-${(slot % 6) + 1}))`;
}

// --- Stat tile ------------------------------------------------------------ //

export interface StatTileProps {
  label: string;
  value: string;
  sub?: ReactNode;
  /** Paint the value in ember — reserve for the one hero figure. */
  accent?: boolean;
  icon?: ReactNode;
  /** Optional 12-ish point trend, drawn in the de-emphasis hue. */
  spark?: number[];
  testId?: string;
}

export function StatTile({
  label,
  value,
  sub,
  accent = false,
  icon,
  spark,
  testId,
}: StatTileProps) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          {icon}
          {label}
        </span>
        {spark && spark.length > 1 ? (
          <Sparkline
            points={spark}
            className={accent ? "text-primary" : "text-muted-foreground/70"}
          />
        ) : null}
      </div>
      <div
        className={cn(
          "font-sans text-2xl font-semibold leading-none tracking-tight sm:text-3xl",
          accent ? "text-primary" : "text-foreground",
        )}
      >
        {value}
      </div>
      {sub ? (
        <div className="text-xs text-muted-foreground">{sub}</div>
      ) : null}
    </div>
  );
}

// --- Sparkline ------------------------------------------------------------ //

export function Sparkline({
  points,
  className,
}: {
  points: number[];
  className?: string;
}) {
  if (points.length < 2) return null;
  const w = 100;
  const h = 28;
  const max = Math.max(...points);
  const min = Math.min(...points);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const d = points
    .map((p, i) => {
      const x = i * step;
      const y = h - 3 - ((p - min) / span) * (h - 6);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg
      className={cn("h-6 w-20 shrink-0 overflow-visible", className)}
      viewBox={`0 0 ${w} ${h}`}
      fill="none"
      aria-hidden
      preserveAspectRatio="none"
    >
      <path
        d={d}
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

// --- Horizontal bar breakdown (magnitude ranking, single hue) ------------- //

export interface BreakdownBar {
  key: string;
  label: string;
  value: number;
  secondary?: string;
}

export function BarBreakdown({
  bars,
  formatValue,
  colorVar = "hsl(var(--chart-1))",
  ariaLabel,
  testId,
}: {
  bars: BreakdownBar[];
  formatValue: (n: number) => string;
  colorVar?: string;
  ariaLabel: string;
  testId?: string;
}) {
  const max = Math.max(1, ...bars.map((b) => b.value));
  return (
    <ul
      data-testid={testId}
      aria-label={ariaLabel}
      className="flex flex-col gap-3"
    >
      {bars.map((bar) => {
        const pct = Math.max(2, (bar.value / max) * 100);
        return (
          <li key={bar.key} className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className="truncate font-medium text-foreground">
                {bar.label}
              </span>
              <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
                {formatValue(bar.value)}
                {bar.secondary ? (
                  <span className="ml-2 text-muted-foreground/70">
                    {bar.secondary}
                  </span>
                ) : null}
              </span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full"
                style={{ width: `${pct}%`, backgroundColor: colorVar }}
                title={`${bar.label}: ${formatValue(bar.value)}`}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// --- Stage latency bars (mean over window) -------------------------------- //

export function StageLatencyBars({
  stages,
  formatValue,
  testId,
}: {
  stages: StageLatency[];
  formatValue: (seconds: number) => string;
  testId?: string;
}) {
  const max = Math.max(1e-6, ...stages.map((s) => s.meanSeconds));
  return (
    <ul
      data-testid={testId}
      aria-label="Retrieval latency by stage"
      className="flex flex-col gap-3"
    >
      {stages.map((s) => {
        const pct = Math.max(2, (s.meanSeconds / max) * 100);
        const isTotal = s.stage === "total";
        return (
          <li key={s.stage} className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span
                className={cn(
                  "truncate capitalize",
                  isTotal ? "font-semibold text-foreground" : "text-foreground",
                )}
              >
                {s.stage}
              </span>
              <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
                {formatValue(s.meanSeconds)}
                <span className="ml-2 text-muted-foreground/70">
                  {compactNumber(s.count)} q
                </span>
              </span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${pct}%`,
                  backgroundColor: isTotal
                    ? "hsl(var(--chart-1))"
                    : "hsl(var(--chart-3))",
                }}
                title={`${s.stage}: ${formatValue(s.meanSeconds)} mean`}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// --- Meter (severity fill on a track) ------------------------------------- //

export type MeterTone = "success" | "warning" | "danger";

export function Meter({
  label,
  valueText,
  fraction,
  tone,
}: {
  label: ReactNode;
  valueText: string;
  fraction: number;
  tone: MeterTone;
}) {
  const pct = Math.max(2, Math.min(100, fraction * 100));
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="truncate font-mono text-xs text-foreground">{label}</span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
          {valueText}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, backgroundColor: `hsl(var(--${tone}))` }}
        />
      </div>
    </div>
  );
}

// --- Trend chart (multi-series line, over time) --------------------------- //

const CHART_W = 760;
const CHART_H = 240;
const PAD = { top: 16, right: 16, bottom: 26, left: 48 };

export function TrendChart({
  series,
  buckets,
  formatValue,
  testId,
}: {
  series: TrendSeries[];
  buckets: string[];
  formatValue: (n: number) => string;
  testId?: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const gradientId = useId();

  const plotW = CHART_W - PAD.left - PAD.right;
  const plotH = CHART_H - PAD.top - PAD.bottom;
  const n = buckets.length;

  const maxY = Math.max(
    1e-9,
    ...series.flatMap((s) => s.points.map((p) => p.value)),
  );
  const niceMax = niceCeil(maxY);

  const xAt = (i: number) =>
    PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v: number) => PAD.top + plotH - (v / niceMax) * plotH;

  const yTicks = [0, 0.5, 1].map((f) => f * niceMax);

  return (
    <div data-testid={testId} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend series={series} />
        <button
          type="button"
          onClick={() => setShowTable((v) => !v)}
          aria-pressed={showTable}
          className={cn(
            "rounded-md border border-border px-2 py-1 text-xs font-medium text-muted-foreground transition-colors",
            "hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          {showTable ? "Hide table" : "Table"}
        </button>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          className="w-full"
          role="img"
          aria-label={`Spend over time across ${series.length} series`}
          onMouseLeave={() => setActive(null)}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(var(--chart-1))" stopOpacity={0.18} />
              <stop offset="100%" stopColor="hsl(var(--chart-1))" stopOpacity={0} />
            </linearGradient>
          </defs>

          {/* Gridlines + y ticks */}
          {yTicks.map((t) => (
            <g key={t}>
              <line
                x1={PAD.left}
                x2={CHART_W - PAD.right}
                y1={yAt(t)}
                y2={yAt(t)}
                stroke="hsl(var(--border))"
                strokeWidth={1}
              />
              <text
                x={PAD.left - 8}
                y={yAt(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-muted-foreground"
                style={{ fontSize: 11 }}
              >
                {formatValue(t)}
              </text>
            </g>
          ))}

          {/* Single-series area wash under slot 1 for a little depth */}
          {series.length === 1 && n > 1 ? (
            <path
              d={`${linePath(series[0], xAt, yAt)} L${xAt(n - 1)} ${yAt(0)} L${xAt(0)} ${yAt(0)} Z`}
              fill={`url(#${gradientId})`}
            />
          ) : null}

          {/* Series lines */}
          {series.map((s) => (
            <path
              key={s.key}
              d={linePath(s, xAt, yAt)}
              fill="none"
              stroke={slotColor(s.slot)}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ))}

          {/* Crosshair + active dots */}
          {active !== null && n > 0 ? (
            <>
              <line
                x1={xAt(active)}
                x2={xAt(active)}
                y1={PAD.top}
                y2={PAD.top + plotH}
                stroke="hsl(var(--spark))"
                strokeWidth={1}
              />
              {series.map((s) => (
                <circle
                  key={s.key}
                  cx={xAt(active)}
                  cy={yAt(s.points[active]?.value ?? 0)}
                  r={4}
                  fill={slotColor(s.slot)}
                  stroke="hsl(var(--card))"
                  strokeWidth={2}
                />
              ))}
            </>
          ) : null}

          {/* x labels — first, middle, last */}
          {n > 0
            ? uniqueTicks(n).map((i) => (
                <text
                  key={i}
                  x={xAt(i)}
                  y={CHART_H - 6}
                  textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                  className="fill-muted-foreground"
                  style={{ fontSize: 11 }}
                >
                  {formatBucketLabel(buckets[i])}
                </text>
              ))
            : null}

          {/* Invisible hover hit-bands (mouse only; the table is the a11y path) */}
          {n > 0
            ? buckets.map((b, i) => (
                <rect
                  key={b}
                  x={xAt(i) - plotW / (2 * Math.max(1, n))}
                  y={PAD.top}
                  width={plotW / Math.max(1, n)}
                  height={plotH}
                  fill="transparent"
                  aria-hidden
                  onMouseEnter={() => setActive(i)}
                />
              ))
            : null}
        </svg>

        {active !== null ? (
          <TrendTooltip
            index={active}
            n={n}
            bucket={buckets[active]}
            series={series}
            formatValue={formatValue}
          />
        ) : null}
      </div>

      {showTable ? (
        <TrendTable
          series={series}
          buckets={buckets}
          formatValue={formatValue}
        />
      ) : null}
    </div>
  );
}

function Legend({ series }: { series: TrendSeries[] }) {
  if (series.length < 2) {
    return (
      <span className="text-xs text-muted-foreground">
        {series[0]?.label ?? ""}
      </span>
    );
  }
  return (
    <ul aria-label="Series legend" className="flex flex-wrap items-center gap-3">
      {series.map((s) => (
        <li
          key={s.key}
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
        >
          <span
            aria-hidden
            className="h-2.5 w-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: slotColor(s.slot) }}
          />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

function TrendTooltip({
  index,
  n,
  bucket,
  series,
  formatValue,
}: {
  index: number;
  n: number;
  bucket: string;
  series: TrendSeries[];
  formatValue: (n: number) => string;
}) {
  // Position by fraction of the plot so no pixel measurement is needed.
  const frac = n <= 1 ? 0.5 : index / (n - 1);
  const style: CSSProperties = {
    left: `${frac * 100}%`,
    transform: `translateX(${frac > 0.6 ? "-100%" : frac < 0.4 ? "0" : "-50%"})`,
  };
  return (
    <div
      data-testid="trend-tooltip"
      role="status"
      style={style}
      className="pointer-events-none absolute top-0 z-10 min-w-32 rounded-md border border-border bg-popover p-2 text-xs shadow-md"
    >
      <div className="mb-1 font-medium text-foreground">
        {formatBucketLabel(bucket)}
      </div>
      <ul className="flex flex-col gap-0.5">
        {series.map((s) => (
          <li key={s.key} className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <span
                aria-hidden
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: slotColor(s.slot) }}
              />
              {s.label}
            </span>
            <span className="font-mono tabular-nums text-foreground">
              {formatValue(s.points[index]?.value ?? 0)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TrendTable({
  series,
  buckets,
  formatValue,
}: {
  series: TrendSeries[];
  buckets: string[];
  formatValue: (n: number) => string;
}) {
  return (
    <div className="max-h-56 overflow-auto rounded-md border border-border">
      <table className="w-full text-xs" aria-label="Spend over time (data table)">
        <thead className="sticky top-0 bg-muted/80 text-muted-foreground backdrop-blur">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">
              Bucket
            </th>
            {series.map((s) => (
              <th
                key={s.key}
                scope="col"
                className="px-3 py-1.5 text-right font-medium"
              >
                {s.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {buckets.map((b, i) => (
            <tr key={b}>
              <th
                scope="row"
                className="whitespace-nowrap px-3 py-1.5 text-left font-normal text-muted-foreground"
              >
                {formatBucketLabel(b)}
              </th>
              {series.map((s) => (
                <td
                  key={s.key}
                  className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground"
                >
                  {formatValue(s.points[i]?.value ?? 0)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- helpers -------------------------------------------------------------- //

function linePath(
  s: TrendSeries,
  xAt: (i: number) => number,
  yAt: (v: number) => number,
): string {
  return s.points
    .map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)} ${yAt(p.value).toFixed(1)}`)
    .join(" ");
}

function uniqueTicks(n: number): number[] {
  if (n <= 1) return [0];
  if (n === 2) return [0, 1];
  return Array.from(new Set([0, Math.floor((n - 1) / 2), n - 1]));
}

/** Round a max up to a clean axis bound (1/2/5 × 10ⁿ). */
function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const frac = v / base;
  const step = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return step * base;
}
