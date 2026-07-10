"use client";

/**
 * Dependency-free chart primitives for the Sprints & velocity screen.
 *
 * Inline SVG styled with Forge design tokens (no chart library, no hardcoded
 * colour) so both charts are theme-aware in light + dark and never touch a CDN.
 * They follow the dataviz mark specs: thin bars with a 3px rounded data-end
 * anchored to the baseline, a 2px surface gap between paired bars, 2px lines, a
 * legend whenever two series are present, and a table view so identity is never
 * carried by colour alone.
 *
 * Palette: the categorical series draw from the repo's already-validated
 * `--chart-*` ramp (ember `--chart-1` = the outcome we care about, steel
 * `--chart-2` = the commitment); the burndown's ideal guide is a muted dashed
 * reference, not a categorical hue.
 */

import { useId, useMemo, useState, type CSSProperties, type ReactNode } from "react";

import type {
  BurndownPoint,
  CFDPoint,
  GoalAlignment,
  MemberAllocation,
  VelocitySprintBar,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  ALLOCATION_STATUS_LABELS,
  allocationStatusBadgeClass,
  formatDateShort,
  formatPct,
  formatPoints,
} from "./sprint-meta";

const COMMITTED_COLOR = "hsl(var(--chart-2))";
const COMPLETED_COLOR = "hsl(var(--chart-1))";
const IDEAL_COLOR = "hsl(var(--muted-foreground))";

/** Categorical ramp the CFD's per-status bands cycle through (token-only). */
const CFD_COLORS = [
  "hsl(var(--chart-2))",
  "hsl(var(--chart-1))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
];

// --- Stat tile ------------------------------------------------------------ //

export interface StatTileProps {
  label: string;
  value: string;
  sub?: ReactNode;
  /** Paint the value in ember — reserve for the one hero figure. */
  accent?: boolean;
  icon?: ReactNode;
  testId?: string;
}

export function StatTile({
  label,
  value,
  sub,
  accent = false,
  icon,
  testId,
}: StatTileProps) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"
    >
      <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        {icon}
        {label}
      </span>
      <div
        className={cn(
          "font-sans text-2xl font-semibold leading-none tracking-tight sm:text-3xl",
          accent ? "text-primary" : "text-foreground",
        )}
      >
        {value}
      </div>
      {sub ? <div className="text-xs text-muted-foreground">{sub}</div> : null}
    </div>
  );
}

// --- Velocity: grouped committed-vs-completed bars ------------------------ //

const V_W = 760;
const V_H = 268;
const V_PAD = { top: 16, right: 16, bottom: 40, left: 44 };

export function VelocityChart({
  bars,
  averageVelocity,
  testId,
}: {
  bars: VelocitySprintBar[];
  /** Rolling mean drawn as a dashed reference line, when > 0. */
  averageVelocity?: number;
  testId?: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  const plotW = V_W - V_PAD.left - V_PAD.right;
  const plotH = V_H - V_PAD.top - V_PAD.bottom;
  const n = bars.length;

  const maxY = Math.max(
    1,
    ...bars.map((b) => Math.max(b.committed_points, b.completed_points)),
    averageVelocity ?? 0,
  );
  const niceMax = niceCeil(maxY);
  const yAt = (v: number) => V_PAD.top + plotH - (v / niceMax) * plotH;
  const yTicks = [0, 0.5, 1].map((f) => f * niceMax);

  const groupW = plotW / Math.max(1, n);
  const barW = Math.min(26, groupW * 0.3);
  const gap = 2; // 2px surface gap between the paired bars
  const groupLeft = (i: number) => V_PAD.left + i * groupW;
  const groupCenter = (i: number) => groupLeft(i) + groupW / 2;

  const avg = averageVelocity && averageVelocity > 0 ? averageVelocity : null;

  return (
    <div data-testid={testId} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend
          items={[
            { key: "committed", label: "Committed", color: COMMITTED_COLOR },
            { key: "completed", label: "Completed", color: COMPLETED_COLOR },
          ]}
        />
        <TableToggle pressed={showTable} onToggle={() => setShowTable((v) => !v)} />
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${V_W} ${V_H}`}
          className="w-full"
          role="img"
          aria-label={`Committed versus completed story points across ${n} sprints`}
          onMouseLeave={() => setActive(null)}
        >
          {yTicks.map((t) => (
            <g key={t}>
              <line
                x1={V_PAD.left}
                x2={V_W - V_PAD.right}
                y1={yAt(t)}
                y2={yAt(t)}
                stroke="hsl(var(--border))"
                strokeWidth={1}
              />
              <text
                x={V_PAD.left - 8}
                y={yAt(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-muted-foreground"
                style={{ fontSize: 11 }}
              >
                {formatPoints(t)}
              </text>
            </g>
          ))}

          {/* Bars */}
          {bars.map((b, i) => {
            const cx = groupCenter(i);
            const committedX = cx - barW - gap / 2;
            const completedX = cx + gap / 2;
            const isActive = active === i;
            return (
              <g key={b.sprint_id} opacity={active === null || isActive ? 1 : 0.55}>
                <path
                  d={barPath(committedX, yAt(b.committed_points), barW, V_PAD.top + plotH - yAt(b.committed_points))}
                  fill={COMMITTED_COLOR}
                />
                <path
                  d={barPath(completedX, yAt(b.completed_points), barW, V_PAD.top + plotH - yAt(b.completed_points))}
                  fill={COMPLETED_COLOR}
                />
              </g>
            );
          })}

          {/* Rolling average reference line */}
          {avg !== null ? (
            <g>
              <line
                x1={V_PAD.left}
                x2={V_W - V_PAD.right}
                y1={yAt(avg)}
                y2={yAt(avg)}
                stroke="hsl(var(--spark))"
                strokeWidth={1.5}
                strokeDasharray="5 4"
              />
              <text
                x={V_W - V_PAD.right}
                y={yAt(avg) - 5}
                textAnchor="end"
                className="fill-muted-foreground"
                style={{ fontSize: 10 }}
              >
                avg {formatPoints(avg)}
              </text>
            </g>
          ) : null}

          {/* x labels */}
          {bars.map((b, i) => (
            <text
              key={b.sprint_id}
              x={groupCenter(i)}
              y={V_H - 8}
              textAnchor="middle"
              className="fill-muted-foreground"
              style={{ fontSize: 11 }}
            >
              {truncate(b.name, 10)}
            </text>
          ))}

          {/* Hover hit-bands (mouse; the table is the a11y path) */}
          {bars.map((b, i) => (
            <rect
              key={b.sprint_id}
              x={groupLeft(i)}
              y={V_PAD.top}
              width={groupW}
              height={plotH}
              fill="transparent"
              aria-hidden
              onMouseEnter={() => setActive(i)}
            />
          ))}
        </svg>

        {active !== null && bars[active] ? (
          <VelocityTooltip
            index={active}
            n={n}
            bar={bars[active]}
          />
        ) : null}
      </div>

      {showTable ? <VelocityTable bars={bars} /> : null}
    </div>
  );
}

function VelocityTooltip({
  index,
  n,
  bar,
}: {
  index: number;
  n: number;
  bar: VelocitySprintBar;
}) {
  const frac = n <= 1 ? 0.5 : (index + 0.5) / n;
  const style: CSSProperties = {
    left: `${frac * 100}%`,
    transform: `translateX(${frac > 0.6 ? "-100%" : frac < 0.4 ? "0" : "-50%"})`,
  };
  return (
    <div
      data-testid="velocity-tooltip"
      role="status"
      style={style}
      className="pointer-events-none absolute top-0 z-10 min-w-40 rounded-md border border-border bg-popover p-2 text-xs shadow-md"
    >
      <div className="mb-1 font-medium text-foreground">{bar.name}</div>
      <ul className="flex flex-col gap-0.5">
        <TooltipRow color={COMMITTED_COLOR} label="Committed" value={`${formatPoints(bar.committed_points)} pts`} />
        <TooltipRow color={COMPLETED_COLOR} label="Completed" value={`${formatPoints(bar.completed_points)} pts`} />
        <li className="mt-0.5 flex items-center justify-between gap-3 border-t border-border pt-0.5 text-muted-foreground">
          <span>Predictability</span>
          <span className="font-mono tabular-nums text-foreground">
            {formatPct(bar.predictability)}
          </span>
        </li>
      </ul>
    </div>
  );
}

function VelocityTable({ bars }: { bars: VelocitySprintBar[] }) {
  return (
    <div className="max-h-56 overflow-auto rounded-md border border-border">
      <table className="w-full text-xs" aria-label="Velocity by sprint (data table)">
        <thead className="sticky top-0 bg-muted/80 text-muted-foreground backdrop-blur">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">Sprint</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Committed</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Completed</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Predictability</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {bars.map((b) => (
            <tr key={b.sprint_id}>
              <th scope="row" className="whitespace-nowrap px-3 py-1.5 text-left font-normal text-muted-foreground">
                {b.name}
              </th>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPoints(b.committed_points)}
              </td>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPoints(b.completed_points)}
              </td>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPct(b.predictability)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Burndown: remaining-vs-ideal line ------------------------------------ //

const B_W = 760;
const B_H = 240;
const B_PAD = { top: 16, right: 16, bottom: 28, left: 44 };

export function BurndownChart({
  points,
  testId,
}: {
  points: BurndownPoint[];
  testId?: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const gradientId = useId();

  const plotW = B_W - B_PAD.left - B_PAD.right;
  const plotH = B_H - B_PAD.top - B_PAD.bottom;
  const n = points.length;

  const maxY = Math.max(
    1,
    ...points.map((p) => Math.max(p.remaining_points, p.ideal_points, p.scope_points)),
  );
  const niceMax = niceCeil(maxY);
  const xAt = (i: number) => B_PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v: number) => B_PAD.top + plotH - (v / niceMax) * plotH;
  const yTicks = [0, 0.5, 1].map((f) => f * niceMax);

  const remainingPath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)} ${yAt(p.remaining_points).toFixed(1)}`)
    .join(" ");
  const idealPath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)} ${yAt(p.ideal_points).toFixed(1)}`)
    .join(" ");

  return (
    <div data-testid={testId} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend
          items={[
            { key: "remaining", label: "Remaining", color: COMPLETED_COLOR },
            { key: "ideal", label: "Ideal", color: IDEAL_COLOR, dashed: true },
          ]}
        />
        <TableToggle pressed={showTable} onToggle={() => setShowTable((v) => !v)} />
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${B_W} ${B_H}`}
          className="w-full"
          role="img"
          aria-label="Remaining story points against the ideal burndown"
          onMouseLeave={() => setActive(null)}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(var(--chart-1))" stopOpacity={0.16} />
              <stop offset="100%" stopColor="hsl(var(--chart-1))" stopOpacity={0} />
            </linearGradient>
          </defs>

          {yTicks.map((t) => (
            <g key={t}>
              <line
                x1={B_PAD.left}
                x2={B_W - B_PAD.right}
                y1={yAt(t)}
                y2={yAt(t)}
                stroke="hsl(var(--border))"
                strokeWidth={1}
              />
              <text
                x={B_PAD.left - 8}
                y={yAt(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-muted-foreground"
                style={{ fontSize: 11 }}
              >
                {formatPoints(t)}
              </text>
            </g>
          ))}

          {/* Area wash under the remaining line */}
          {n > 1 ? (
            <path
              d={`${remainingPath} L${xAt(n - 1)} ${yAt(0)} L${xAt(0)} ${yAt(0)} Z`}
              fill={`url(#${gradientId})`}
            />
          ) : null}

          {/* Ideal reference (dashed, muted) */}
          {n > 1 ? (
            <path
              d={idealPath}
              fill="none"
              stroke={IDEAL_COLOR}
              strokeWidth={1.5}
              strokeDasharray="5 4"
              strokeLinecap="round"
            />
          ) : null}

          {/* Remaining (solid ember) */}
          {n > 1 ? (
            <path
              d={remainingPath}
              fill="none"
              stroke={COMPLETED_COLOR}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ) : null}

          {/* Crosshair + active dots */}
          {active !== null && n > 0 ? (
            <>
              <line
                x1={xAt(active)}
                x2={xAt(active)}
                y1={B_PAD.top}
                y2={B_PAD.top + plotH}
                stroke="hsl(var(--spark))"
                strokeWidth={1}
              />
              <circle
                cx={xAt(active)}
                cy={yAt(points[active]?.remaining_points ?? 0)}
                r={4}
                fill={COMPLETED_COLOR}
                stroke="hsl(var(--card))"
                strokeWidth={2}
              />
            </>
          ) : null}

          {/* x labels — first, middle, last */}
          {n > 0
            ? uniqueTicks(n).map((i) => (
                <text
                  key={i}
                  x={xAt(i)}
                  y={B_H - 8}
                  textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                  className="fill-muted-foreground"
                  style={{ fontSize: 11 }}
                >
                  {formatDateShort(points[i]?.snapshot_date)}
                </text>
              ))
            : null}

          {/* Hover hit-bands */}
          {n > 0
            ? points.map((p, i) => (
                <rect
                  key={p.snapshot_date}
                  x={xAt(i) - plotW / (2 * Math.max(1, n))}
                  y={B_PAD.top}
                  width={plotW / Math.max(1, n)}
                  height={plotH}
                  fill="transparent"
                  aria-hidden
                  onMouseEnter={() => setActive(i)}
                />
              ))
            : null}
        </svg>

        {active !== null && points[active] ? (
          <BurndownTooltip index={active} n={n} point={points[active]} />
        ) : null}
      </div>

      {showTable ? <BurndownTable points={points} /> : null}
    </div>
  );
}

function BurndownTooltip({
  index,
  n,
  point,
}: {
  index: number;
  n: number;
  point: BurndownPoint;
}) {
  const frac = n <= 1 ? 0.5 : index / (n - 1);
  const style: CSSProperties = {
    left: `${frac * 100}%`,
    transform: `translateX(${frac > 0.6 ? "-100%" : frac < 0.4 ? "0" : "-50%"})`,
  };
  return (
    <div
      data-testid="burndown-tooltip"
      role="status"
      style={style}
      className="pointer-events-none absolute top-0 z-10 min-w-36 rounded-md border border-border bg-popover p-2 text-xs shadow-md"
    >
      <div className="mb-1 font-medium text-foreground">
        {formatDateShort(point.snapshot_date)}
      </div>
      <ul className="flex flex-col gap-0.5">
        <TooltipRow color={COMPLETED_COLOR} label="Remaining" value={`${formatPoints(point.remaining_points)} pts`} />
        <TooltipRow color={IDEAL_COLOR} label="Ideal" value={`${formatPoints(point.ideal_points)} pts`} dashed />
      </ul>
    </div>
  );
}

function BurndownTable({ points }: { points: BurndownPoint[] }) {
  return (
    <div className="max-h-56 overflow-auto rounded-md border border-border">
      <table className="w-full text-xs" aria-label="Burndown by day (data table)">
        <thead className="sticky top-0 bg-muted/80 text-muted-foreground backdrop-blur">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">Day</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Remaining</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Ideal</th>
            <th scope="col" className="px-3 py-1.5 text-right font-medium">Scope</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {points.map((p) => (
            <tr key={p.snapshot_date}>
              <th scope="row" className="whitespace-nowrap px-3 py-1.5 text-left font-normal text-muted-foreground">
                {formatDateShort(p.snapshot_date)}
              </th>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPoints(p.remaining_points)}
              </td>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPoints(p.ideal_points)}
              </td>
              <td className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                {formatPoints(p.scope_points)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Capacity: per-member assigned-vs-declared bars (F40 PM depth) -------- //

/**
 * One horizontal bar per member: assigned points against declared capacity,
 * coloured by allocation status (token-only — success/warning/danger, the
 * same tri-state ramp the predictability KPI uses). No hardcoded hex.
 */
export function CapacityBars({
  members,
  testId,
}: {
  members: MemberAllocation[];
  testId?: string;
}) {
  if (members.length === 0) return null;
  const maxPoints = Math.max(1, ...members.map((m) => Math.max(m.capacity_points, m.assigned_points)));
  return (
    <ul data-testid={testId} className="flex flex-col gap-3">
      {members.map((m) => {
        const pct = Math.min(100, Math.round((m.assigned_points / maxPoints) * 100));
        const capPct = Math.min(100, Math.round((m.capacity_points / maxPoints) * 100));
        return (
          <li key={m.member_id} className="flex flex-col gap-1">
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="truncate font-medium text-foreground">{m.member_id}</span>
              <span
                className={cn(
                  "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 font-medium",
                  allocationStatusBadgeClass(m.status),
                )}
              >
                {ALLOCATION_STATUS_LABELS[m.status]}
              </span>
            </div>
            <div className="relative h-2 rounded-full bg-muted">
              <span
                aria-hidden
                className="absolute inset-y-0 left-0 rounded-full border-r-2 border-foreground/40"
                style={{ width: `${capPct}%` }}
              />
              <span
                aria-hidden
                className={cn(
                  "absolute inset-y-0 left-0 rounded-full",
                  m.status === "over"
                    ? "bg-danger"
                    : m.status === "under"
                      ? "bg-warning"
                      : "bg-success",
                )}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                {formatPoints(m.assigned_points)} / {formatPoints(m.capacity_points)} pts
              </span>
              <span className="font-mono tabular-nums">{formatPct(m.utilization)}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// --- Goal alignment: sprint-goal <-> acceptance-criteria coverage (F40) --- //

/**
 * The sprint goal's keyword coverage across its committed tasks: a ratio
 * meter plus the count of tasks that share no meaningful token with the
 * goal. Token-only colour (success/warning/danger on the same band the
 * predictability KPI uses) — no hardcoded hex.
 */
export function GoalAlignmentMeter({
  alignment,
  testId,
}: {
  alignment: GoalAlignment;
  testId?: string;
}) {
  const pct = Math.min(100, Math.round(alignment.alignment_ratio * 100));
  const tone =
    alignment.alignment_ratio >= 0.9
      ? "success"
      : alignment.alignment_ratio >= 0.6
        ? "warning"
        : "danger";
  return (
    <div data-testid={testId} className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="text-muted-foreground">
          {alignment.aligned_count} of {alignment.total_count} tasks aligned
        </span>
        <span
          className={cn(
            "font-mono tabular-nums",
            tone === "success"
              ? "text-success"
              : tone === "warning"
                ? "text-warning"
                : "text-danger",
          )}
        >
          {formatPct(alignment.alignment_ratio)}
        </span>
      </div>
      <div className="h-2 rounded-full bg-muted">
        <span
          aria-hidden
          className={cn(
            "block h-full rounded-full",
            tone === "success" ? "bg-success" : tone === "warning" ? "bg-warning" : "bg-danger",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      {alignment.unaligned_task_ids.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          {alignment.unaligned_task_ids.length}{" "}
          {alignment.unaligned_task_ids.length === 1 ? "task shares" : "tasks share"} no
          keyword with the goal.
        </p>
      ) : null}
    </div>
  );
}

// --- Portfolio: Cumulative Flow Diagram (F40 PM depth) --------------------- //

const CFD_W = 760;
const CFD_H = 240;
const CFD_PAD = { top: 16, right: 16, bottom: 28, left: 44 };

/**
 * Stacked-area task count per status over a date range. Statuses keep a
 * stable order (first-seen across the series) so a band's colour never
 * shifts day to day; each is drawn from the validated `--chart-*` ramp.
 */
export function CFDChart({ points, testId }: { points: CFDPoint[]; testId?: string }) {
  const [showTable, setShowTable] = useState(false);

  const statuses = useMemo(() => {
    const seen: string[] = [];
    for (const p of points) {
      for (const key of Object.keys(p.status_counts)) {
        if (!seen.includes(key)) seen.push(key);
      }
    }
    return seen;
  }, [points]);

  const n = points.length;
  const plotW = CFD_W - CFD_PAD.left - CFD_PAD.right;
  const plotH = CFD_H - CFD_PAD.top - CFD_PAD.bottom;

  // Cumulative stack per day: stacks[i] is one running total per status.
  const stacks = points.map((p) => {
    let running = 0;
    return statuses.map((s) => {
      running += p.status_counts[s] ?? 0;
      return running;
    });
  });
  const maxY = Math.max(1, ...stacks.map((s) => s[s.length - 1] ?? 0));
  const niceMax = niceCeil(maxY);
  const xAt = (i: number) => CFD_PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v: number) => CFD_PAD.top + plotH - (v / niceMax) * plotH;
  const yTicks = [0, 0.5, 1].map((f) => f * niceMax);

  const bandPath = (statusIndex: number): string => {
    if (n < 2) return "";
    const top = points.map((_p, i) => stacks[i]?.[statusIndex] ?? 0);
    const bottom = points.map((_p, i) =>
      statusIndex === 0 ? 0 : (stacks[i]?.[statusIndex - 1] ?? 0),
    );
    const upper = top.map(
      (v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`,
    );
    const lower = bottom
      .map((v, i) => `L${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`)
      .reverse();
    return `${upper.join(" ")} ${lower.join(" ")} Z`;
  };

  if (statuses.length === 0 || n === 0) return null;

  return (
    <div data-testid={testId} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend
          items={statuses.map((s, i) => ({
            key: s,
            label: s,
            color: CFD_COLORS[i % CFD_COLORS.length] as string,
          }))}
        />
        <TableToggle pressed={showTable} onToggle={() => setShowTable((v) => !v)} />
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${CFD_W} ${CFD_H}`}
          className="w-full"
          role="img"
          aria-label={`Cumulative flow diagram across ${n} days and ${statuses.length} statuses`}
        >
          {yTicks.map((t) => (
            <g key={t}>
              <line
                x1={CFD_PAD.left}
                x2={CFD_W - CFD_PAD.right}
                y1={yAt(t)}
                y2={yAt(t)}
                stroke="hsl(var(--border))"
                strokeWidth={1}
              />
              <text
                x={CFD_PAD.left - 8}
                y={yAt(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-muted-foreground"
                style={{ fontSize: 11 }}
              >
                {formatPoints(t)}
              </text>
            </g>
          ))}

          {statuses.map((s, i) => (
            <path key={s} d={bandPath(i)} fill={CFD_COLORS[i % CFD_COLORS.length]} opacity={0.85} />
          ))}

          {n > 0
            ? uniqueTicks(n).map((i) => (
                <text
                  key={i}
                  x={xAt(i)}
                  y={CFD_H - 8}
                  textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                  className="fill-muted-foreground"
                  style={{ fontSize: 11 }}
                >
                  {formatDateShort(points[i]?.snapshot_date)}
                </text>
              ))
            : null}
        </svg>
      </div>

      {showTable ? <CFDTable points={points} statuses={statuses} /> : null}
    </div>
  );
}

function CFDTable({ points, statuses }: { points: CFDPoint[]; statuses: string[] }) {
  return (
    <div className="max-h-56 overflow-auto rounded-md border border-border">
      <table className="w-full text-xs" aria-label="Cumulative flow by day (data table)">
        <thead className="sticky top-0 bg-muted/80 text-muted-foreground backdrop-blur">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">Day</th>
            {statuses.map((s) => (
              <th key={s} scope="col" className="px-3 py-1.5 text-right font-medium">
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {points.map((p) => (
            <tr key={p.snapshot_date}>
              <th scope="row" className="whitespace-nowrap px-3 py-1.5 text-left font-normal text-muted-foreground">
                {formatDateShort(p.snapshot_date)}
              </th>
              {statuses.map((s) => (
                <td key={s} className="px-3 py-1.5 text-right font-mono tabular-nums text-foreground">
                  {formatPoints(p.status_counts[s] ?? 0)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Shared bits ---------------------------------------------------------- //

interface LegendItem {
  key: string;
  label: string;
  color: string;
  dashed?: boolean;
}

function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul aria-label="Series legend" className="flex flex-wrap items-center gap-3">
      {items.map((it) => (
        <li key={it.key} className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {it.dashed ? (
            <span
              aria-hidden
              className="h-0 w-3.5 shrink-0 border-t-2 border-dashed"
              style={{ borderColor: it.color }}
            />
          ) : (
            <span
              aria-hidden
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: it.color }}
            />
          )}
          {it.label}
        </li>
      ))}
    </ul>
  );
}

function TooltipRow({
  color,
  label,
  value,
  dashed,
}: {
  color: string;
  label: string;
  value: string;
  dashed?: boolean;
}) {
  return (
    <li className="flex items-center justify-between gap-3">
      <span className="flex items-center gap-1.5 text-muted-foreground">
        {dashed ? (
          <span aria-hidden className="h-0 w-2.5 border-t-2 border-dashed" style={{ borderColor: color }} />
        ) : (
          <span aria-hidden className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
        )}
        {label}
      </span>
      <span className="font-mono tabular-nums text-foreground">{value}</span>
    </li>
  );
}

function TableToggle({
  pressed,
  onToggle,
}: {
  pressed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={pressed}
      className={cn(
        "rounded-md border border-border px-2 py-1 text-xs font-medium text-muted-foreground transition-colors",
        "hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
    >
      {pressed ? "Hide table" : "Table"}
    </button>
  );
}

// --- helpers -------------------------------------------------------------- //

/** Top-rounded rect path anchored to the baseline (bars grow up from y+h). */
function barPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(3, w / 2, h);
  if (h <= 0) return "";
  const bottom = y + h;
  return [
    `M${x} ${bottom}`,
    `L${x} ${y + r}`,
    `Q${x} ${y} ${x + r} ${y}`,
    `L${x + w - r} ${y}`,
    `Q${x + w} ${y} ${x + w} ${y + r}`,
    `L${x + w} ${bottom}`,
    "Z",
  ].join(" ");
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
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
