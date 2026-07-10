"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Boxes,
  CalendarRange,
  Flag,
  Gauge,
  KanbanSquare,
  Layers,
  LineChart,
  Play,
  Target,
  Timer,
  TrendingUp,
  Users,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { KanbanBoard } from "@/components/board/kanban-board";
import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { queryKeys, useSetTaskStatus } from "@/lib/api/hooks";
import {
  useCompleteSprint,
  useGoalAlignment,
  usePortfolioVelocity,
  useProjectCfd,
  useProjectCycleLeadTime,
  useProjectSprints,
  useSprintBurndown,
  useSprintCapacity,
  useStartSprint,
  useVelocityDashboard,
} from "@/lib/api/sprints";
import type { Sprint, TaskDTO, TaskStatus } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  BurndownChart,
  CapacityBars,
  CFDChart,
  GoalAlignmentMeter,
  StatTile,
  VelocityChart,
} from "./sprint-charts";
import {
  formatDateShort,
  formatDecimal,
  formatPct,
  formatPoints,
  predictabilityTone,
  SPRINT_STATE_LABELS,
  sortSprintsNewestFirst,
  sprintStateBadgeClass,
  pickDefaultSprintId,
  type Tone,
} from "./sprint-meta";

/** Placeholder project until project routing lands (mirrors the spec screen). */
export const DEFAULT_PROJECT_ID = "default";

const VELOCITY_WINDOW = 6;

export interface SprintsViewProps {
  projectId?: string;
  client?: ForgeApiClient;
}

/**
 * Sprints & velocity (F26). One project-scoped workspace for a sprint: its board
 * (move tasks across the workflow, optimistically), the committed-vs-completed
 * velocity trend with a rolling-average guide, and the day-by-day burndown
 * against the ideal. Keyboard-first — the sprint picker is a native select, the
 * command palette starts/completes the focused sprint, and the single ember
 * action drives the sprint's lifecycle.
 */
export function SprintsView({
  projectId = DEFAULT_PROJECT_ID,
  client = apiClient,
}: SprintsViewProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const sprintsQuery = useProjectSprints(projectId, client);
  const velocityQuery = useVelocityDashboard(projectId, VELOCITY_WINDOW, client);

  const sprints = useMemo(
    () => sprintsQuery.data ?? [],
    [sprintsQuery.data],
  );
  const ordered = useMemo(() => sortSprintsNewestFirst(sprints), [sprints]);

  // Effective selection derived during render: honour an explicit pick that is
  // still present, else the running/most-recent sprint.
  const effectiveId =
    picked && sprints.some((s) => s.id === picked)
      ? picked
      : pickDefaultSprintId(sprints);
  const sprint = sprints.find((s) => s.id === effectiveId) ?? null;

  const burndownQuery = useSprintBurndown(effectiveId, client);
  const capacityQuery = useSprintCapacity(effectiveId, client);
  const goalAlignmentQuery = useGoalAlignment(effectiveId, client);

  // Cumulative Flow Diagram window: the focused sprint's own dates, else a
  // trailing-30-day fallback so the panel still has something to query.
  const fallbackCfdWindow = useMemo(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
    return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)] as const;
  }, []);
  const [cfdStart, cfdEnd] =
    sprint?.start_date && sprint?.end_date
      ? ([sprint.start_date, sprint.end_date] as const)
      : fallbackCfdWindow;
  const cfdQuery = useProjectCfd(projectId, cfdStart, cfdEnd, client);
  const cycleLeadTimeQuery = useProjectCycleLeadTime(projectId, client);
  const portfolioQuery = usePortfolioVelocity([projectId], VELOCITY_WINDOW, client);

  const tasksQuery = useQuery({
    queryKey: queryKeys.tasks({ sprint_id: effectiveId ?? "" }),
    queryFn: () => client.listTasks({ sprint_id: effectiveId as string }),
    enabled: Boolean(effectiveId),
  });

  const setTaskStatus = useSetTaskStatus(client);
  const startSprint = useStartSprint(client);
  const completeSprint = useCompleteSprint(client);

  const onMoveTask = (taskId: string, next: TaskStatus) => {
    setTaskStatus.mutate(
      { taskId, status: next },
      {
        onError: () => setStatus("Could not move the task — the change was rolled back."),
      },
    );
  };

  const onStart = () => {
    if (!sprint || sprint.state !== "planned") return;
    startSprint.mutate(sprint.id, {
      onSuccess: () => setStatus(`Started ${sprint.name}.`),
      onError: () => setStatus("Could not start the sprint."),
    });
  };

  const onComplete = () => {
    if (!sprint || sprint.state !== "active") return;
    completeSprint.mutate(
      { sprintId: sprint.id, body: { carryover: "backlog" } },
      {
        onSuccess: () => setStatus(`Completed ${sprint.name}. Carryover returned to the backlog.`),
        onError: () => setStatus("Could not complete the sprint."),
      },
    );
  };

  // --- command palette (stable command list -> latest handlers via refs) --- //
  const startRef = useRef(onStart);
  const completeRef = useRef(onComplete);
  useEffect(() => {
    startRef.current = onStart;
    completeRef.current = onComplete;
  });
  const commands = useMemo(
    () => [
      {
        id: "sprint-start",
        label: "Start sprint",
        group: "Sprints",
        icon: <Play />,
        run: () => startRef.current(),
      },
      {
        id: "sprint-complete",
        label: "Complete sprint",
        group: "Sprints",
        icon: <Flag />,
        run: () => completeRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("sprints", commands);

  const velocity = velocityQuery.data;
  const lifecycleBusy = startSprint.isPending || completeSprint.isPending;

  return (
    <div
      data-testid="sprints"
      role="region"
      aria-label="Sprints and velocity"
      className="flex h-full flex-col gap-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <TrendingUp className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight">
              Sprints &amp; velocity
            </h1>
            <p className="text-sm text-muted-foreground">
              Move work across the board, then watch it land in velocity and burndown.
            </p>
          </div>
        </div>

        {sprint ? (
          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor="sprint-picker">
              Focused sprint
            </label>
            <select
              id="sprint-picker"
              data-testid="sprint-picker"
              value={sprint.id}
              onChange={(e) => setPicked(e.target.value)}
              className={cn(
                "h-9 rounded-md border border-border bg-card px-2.5 text-sm font-medium text-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
            >
              {ordered.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} · {SPRINT_STATE_LABELS[s.state]}
                </option>
              ))}
            </select>
            <LifecycleAction
              sprint={sprint}
              busy={lifecycleBusy}
              onStart={onStart}
              onComplete={onComplete}
            />
          </div>
        ) : null}
      </header>

      <span data-testid="sprints-status" role="status" aria-live="polite" className="sr-only">
        {status}
      </span>

      {sprintsQuery.isLoading ? (
        <BoardSkeleton />
      ) : sprintsQuery.isError ? (
        <ScreenError />
      ) : sprints.length === 0 ? (
        <NoSprints />
      ) : sprint ? (
        <div className="flex flex-col gap-5">
          <SprintSummary sprint={sprint} />

          {/* KPI row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
            <StatTile
              testId="kpi-committed"
              label="Committed"
              value={`${formatPoints(sprint.committed_points)} pts`}
              icon={<Target className="h-3.5 w-3.5" aria-hidden />}
              sub={`${formatPoints(sprint.committed_task_count)} ${sprint.committed_task_count === 1 ? "task" : "tasks"}`}
            />
            <StatTile
              testId="kpi-completed"
              label="Completed"
              value={`${formatPoints(sprint.completed_points)} pts`}
              icon={<KanbanSquare className="h-3.5 w-3.5" aria-hidden />}
              sub={`${formatPoints(sprint.remaining_points)} remaining`}
            />
            <StatTile
              testId="kpi-predictability"
              label="Predictability"
              value={formatPct(sprint.predictability)}
              accent
              icon={<Gauge className="h-3.5 w-3.5" aria-hidden />}
              sub={<ToneNote tone={predictabilityTone(sprint.predictability)} />}
            />
            <StatTile
              testId="kpi-scope"
              label="Scope change"
              value={formatPct(sprint.scope_change_ratio)}
              icon={<TrendingUp className="h-3.5 w-3.5" aria-hidden />}
              sub={`+${formatPoints(sprint.added_points)} · −${formatPoints(sprint.removed_points)} pts`}
            />
            <StatTile
              testId="kpi-forecast"
              label="Forecast"
              value={velocity ? `${formatDecimal(velocity.summary.forecast_avg)} pts` : "—"}
              icon={<LineChart className="h-3.5 w-3.5" aria-hidden />}
              sub={
                velocity
                  ? `${formatDecimal(velocity.summary.forecast_low)}–${formatDecimal(velocity.summary.forecast_high)} range`
                  : "needs history"
              }
            />
          </div>

          {/* Sprint board — move tasks across the workflow */}
          <Panel
            title="Sprint board"
            icon={<KanbanSquare className="h-4 w-4" aria-hidden />}
            action={
              <span className="text-xs text-muted-foreground">
                Move a card with its ‹ › controls
              </span>
            }
          >
            <SprintBoard
              isLoading={tasksQuery.isLoading}
              isError={tasksQuery.isError}
              tasks={tasksQuery.data ?? []}
              onMoveTask={onMoveTask}
            />
          </Panel>

          {/* Velocity + burndown */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel
              title="Velocity"
              icon={<TrendingUp className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">last {VELOCITY_WINDOW} sprints</span>
              }
            >
              {velocityQuery.isLoading ? (
                <ChartSkeleton testId="velocity-skeleton" />
              ) : velocityQuery.isError ? (
                <PanelNote testId="velocity-error">
                  Velocity is unavailable right now.
                </PanelNote>
              ) : velocity && velocity.sprints.length > 0 ? (
                <VelocityChart
                  testId="velocity-chart"
                  bars={velocity.sprints}
                  averageVelocity={
                    velocity.summary.rolling_3_velocity ||
                    velocity.summary.average_velocity
                  }
                />
              ) : (
                <EmptyState
                  testId="empty-velocity"
                  icon={<TrendingUp className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No velocity yet"
                  body="Complete a sprint to chart committed against completed points."
                />
              )}
            </Panel>

            <Panel
              title="Burndown"
              icon={<LineChart className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">{sprint.name}</span>
              }
            >
              {burndownQuery.isLoading ? (
                <ChartSkeleton testId="burndown-skeleton" />
              ) : burndownQuery.isError ? (
                <PanelNote testId="burndown-error">
                  Burndown is unavailable right now.
                </PanelNote>
              ) : burndownQuery.data && burndownQuery.data.points.length > 1 ? (
                <BurndownChart testId="burndown-chart" points={burndownQuery.data.points} />
              ) : (
                <EmptyState
                  testId="empty-burndown"
                  icon={<LineChart className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No burndown yet"
                  body={
                    sprint.state === "planned"
                      ? "Start the sprint to begin snapshotting remaining work each day."
                      : "Daily snapshots will chart here as work burns down."
                  }
                />
              )}
            </Panel>
          </div>

          {/* Per-member capacity + sprint-goal alignment (F40 PM depth) */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel
              title="Capacity"
              icon={<Users className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">assigned vs. declared</span>
              }
            >
              {capacityQuery.isLoading ? (
                <ChartSkeleton testId="capacity-skeleton" />
              ) : capacityQuery.isError ? (
                <PanelNote testId="capacity-error">Capacity is unavailable right now.</PanelNote>
              ) : capacityQuery.data && capacityQuery.data.members.length > 0 ? (
                <CapacityBars testId="capacity-bars" members={capacityQuery.data.members} />
              ) : (
                <EmptyState
                  testId="empty-capacity"
                  icon={<Users className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No capacity declared"
                  body="Declare each member's capacity for this sprint to see over/under-allocation."
                />
              )}
            </Panel>

            <Panel
              title="Goal alignment"
              icon={<Target className="h-4 w-4" aria-hidden />}
              action={
                <span className="text-xs text-muted-foreground">goal vs. committed tasks</span>
              }
            >
              {goalAlignmentQuery.isLoading ? (
                <ChartSkeleton testId="goal-alignment-skeleton" />
              ) : goalAlignmentQuery.isError ? (
                <PanelNote testId="goal-alignment-error">
                  Goal alignment is unavailable right now.
                </PanelNote>
              ) : goalAlignmentQuery.data && goalAlignmentQuery.data.total_count > 0 ? (
                <GoalAlignmentMeter
                  testId="goal-alignment-meter"
                  alignment={goalAlignmentQuery.data}
                />
              ) : (
                <EmptyState
                  testId="empty-goal-alignment"
                  icon={<Target className="h-7 w-7 text-muted-foreground" aria-hidden />}
                  title="No goal to score"
                  body="Set a sprint goal and commit tasks to see how well they line up."
                />
              )}
            </Panel>
          </div>

          {/* Portfolio: Cumulative Flow Diagram + cycle/lead time (F40 PM depth) */}
          <Panel
            title="Cumulative flow"
            icon={<Layers className="h-4 w-4" aria-hidden />}
            action={
              <span className="text-xs text-muted-foreground">
                {formatDateShort(cfdStart)} – {formatDateShort(cfdEnd)}
              </span>
            }
          >
            <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
              <StatTile
                testId="kpi-lead-time"
                label="Avg lead time"
                value={
                  cycleLeadTimeQuery.data
                    ? `${formatDecimal(cycleLeadTimeQuery.data.average_lead_time_days)}d`
                    : "—"
                }
                icon={<Timer className="h-3.5 w-3.5" aria-hidden />}
              />
              <StatTile
                testId="kpi-cycle-time"
                label="Avg cycle time"
                value={
                  cycleLeadTimeQuery.data
                    ? `${formatDecimal(cycleLeadTimeQuery.data.average_cycle_time_days)}d`
                    : "—"
                }
                icon={<Timer className="h-3.5 w-3.5" aria-hidden />}
              />
              <StatTile
                testId="kpi-portfolio-forecast"
                label="Portfolio forecast"
                value={
                  portfolioQuery.data
                    ? `${formatDecimal(portfolioQuery.data.total_forecast_avg)} pts`
                    : "—"
                }
                icon={<Boxes className="h-3.5 w-3.5" aria-hidden />}
              />
            </div>
            {cfdQuery.isLoading ? (
              <ChartSkeleton testId="cfd-skeleton" />
            ) : cfdQuery.isError ? (
              <PanelNote testId="cfd-error">Cumulative flow is unavailable right now.</PanelNote>
            ) : cfdQuery.data && cfdQuery.data.points.length > 1 ? (
              <CFDChart testId="cfd-chart" points={cfdQuery.data.points} />
            ) : (
              <EmptyState
                testId="empty-cfd"
                icon={<Layers className="h-7 w-7 text-muted-foreground" aria-hidden />}
                title="No flow data yet"
                body="Status changes will chart here as tasks move across the workflow."
              />
            )}
          </Panel>
        </div>
      ) : null}
    </div>
  );
}

// --- Lifecycle action (the one ember button) ------------------------------ //

function LifecycleAction({
  sprint,
  busy,
  onStart,
  onComplete,
}: {
  sprint: Sprint;
  busy: boolean;
  onStart: () => void;
  onComplete: () => void;
}) {
  if (sprint.state === "planned") {
    return (
      <Button data-testid="lifecycle-action" onClick={onStart} disabled={busy}>
        <Play className="h-4 w-4" aria-hidden />
        Start sprint
      </Button>
    );
  }
  if (sprint.state === "active") {
    return (
      <Button data-testid="lifecycle-action" onClick={onComplete} disabled={busy}>
        <Flag className="h-4 w-4" aria-hidden />
        Complete sprint
      </Button>
    );
  }
  return (
    <span
      data-testid="lifecycle-done"
      className={cn(
        "inline-flex items-center rounded-md border px-2.5 py-1.5 text-xs font-medium",
        sprintStateBadgeClass(sprint.state),
      )}
    >
      {SPRINT_STATE_LABELS[sprint.state]}
    </span>
  );
}

// --- Sprint summary strip ------------------------------------------------- //

function SprintSummary({ sprint }: { sprint: Sprint }) {
  return (
    <section className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <h2 className="truncate font-display text-base font-semibold tracking-tight text-foreground">
          {sprint.name}
        </h2>
        <span
          className={cn(
            "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-xs font-medium",
            sprintStateBadgeClass(sprint.state),
          )}
        >
          {SPRINT_STATE_LABELS[sprint.state]}
        </span>
      </div>
      <div className="flex items-center gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <CalendarRange className="h-3.5 w-3.5" aria-hidden />
          {formatDateShort(sprint.start_date)} → {formatDateShort(sprint.end_date)}
        </span>
        {sprint.goal ? (
          <span className="hidden max-w-xs truncate sm:inline" title={sprint.goal}>
            {sprint.goal}
          </span>
        ) : null}
      </div>
    </section>
  );
}

function ToneNote({ tone }: { tone: Tone }) {
  const label = tone === "success" ? "On target" : tone === "warning" ? "Drifting" : "Off pace";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: `hsl(var(--${tone}))` }}
      />
      {label}
    </span>
  );
}

// --- Sprint board ---------------------------------------------------------- //

function SprintBoard({
  isLoading,
  isError,
  tasks,
  onMoveTask,
}: {
  isLoading: boolean;
  isError: boolean;
  tasks: TaskDTO[];
  onMoveTask: (taskId: string, status: TaskStatus) => void;
}) {
  if (isLoading) {
    return (
      <div data-testid="board-skeleton" aria-busy="true" className="flex gap-3 overflow-hidden">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="h-56 w-72 shrink-0 animate-pulse rounded-md border border-border bg-muted/40" />
        ))}
      </div>
    );
  }
  if (isError) {
    return <PanelNote testId="board-error">The sprint board could not load.</PanelNote>;
  }
  if (tasks.length === 0) {
    return (
      <EmptyState
        testId="empty-board"
        icon={<KanbanSquare className="h-7 w-7 text-muted-foreground" aria-hidden />}
        title="No tasks in this sprint"
        body="Assign tasks to this sprint from the board to plan the work."
      />
    );
  }
  return (
    <div className="h-[26rem]">
      <KanbanBoard tasks={tasks} onStatusChange={onMoveTask} />
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

function ChartSkeleton({ testId }: { testId: string }) {
  return (
    <div data-testid={testId} aria-busy="true" className="flex flex-col gap-3">
      <div className="h-3 w-24 animate-pulse rounded bg-muted" />
      <div className="h-56 w-full animate-pulse rounded-md bg-muted/50" />
    </div>
  );
}

function BoardSkeleton() {
  return (
    <div data-testid="sprints-skeleton" aria-busy="true" className="flex flex-col gap-5">
      <div className="h-14 animate-pulse rounded-lg border border-border bg-card" />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg border border-border bg-card" />
        ))}
      </div>
      <div className="h-64 animate-pulse rounded-lg border border-border bg-card" />
    </div>
  );
}

function ScreenError() {
  return (
    <div
      role="status"
      data-testid="sprints-error"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <TrendingUp className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">Sprints unavailable</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        The sprint service is unreachable — the board and velocity will return
        once it is back.
      </p>
    </div>
  );
}

function NoSprints() {
  return (
    <div
      data-testid="sprints-empty"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <CalendarRange className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">No sprints yet</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        Plan a sprint for this project to start tracking its board, velocity and
        burndown. New sprints appear here the moment they are created.
      </p>
    </div>
  );
}
