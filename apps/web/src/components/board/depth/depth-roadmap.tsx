"use client";

import { Flag } from "lucide-react";

import type {
  EpicDTO,
  MilestoneDTO,
  SprintDTO,
  TaskDTO,
  TaskStatus,
} from "@/lib/api/types";
import { STATUS_LABELS } from "@/lib/board/status";
import { buildRoadmap, type RoadmapColumn } from "@/lib/board/roadmap";
import { cn } from "@/lib/utils";

export interface DepthRoadmapProps {
  tasks: TaskDTO[];
  epics: EpicDTO[];
  sprints: SprintDTO[];
  milestones: MilestoneDTO[];
}

/** Semantic colour per status dot (tokens only — no literals). */
const STATUS_DOT: Record<TaskStatus, string> = {
  backlog: "bg-muted-foreground",
  ready: "bg-secondary-foreground",
  ready_for_agent: "bg-spark",
  in_progress: "bg-primary",
  in_review: "bg-warning",
  blocked: "bg-danger",
  done: "bg-success",
  cancelled: "bg-muted-foreground",
};

function formatRange(column: RoadmapColumn): string | null {
  if (!column.startsAt && !column.endsAt) {
    return null;
  }
  const fmt = (iso: string | null) => {
    if (!iso) {
      return "…";
    }
    const t = Date.parse(iso);
    if (Number.isNaN(t)) {
      return "…";
    }
    return new Date(t).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  };
  return `${fmt(column.startsAt)} – ${fmt(column.endsAt)}`;
}

/**
 * Roadmap view. Sprints are the time skeleton (left→right by start date),
 * milestones pin above the sprint window that contains their due date, and epics
 * are swimlanes. Each cell shows the tasks scheduled in that epic × sprint.
 */
export function DepthRoadmap({
  tasks,
  epics,
  sprints,
  milestones,
}: DepthRoadmapProps) {
  const roadmap = buildRoadmap({ tasks, epics, sprints, milestones });

  if (roadmap.total === 0) {
    return (
      <div
        data-testid="roadmap-empty"
        className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border p-12 text-center"
      >
        <Flag aria-hidden className="h-6 w-6 text-muted-foreground" />
        <p className="text-sm font-medium">Nothing scheduled yet</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Assign tasks to a sprint and epic to see them laid out on the roadmap.
        </p>
      </div>
    );
  }

  const columnWidth = "12rem";
  const laneWidth = "11rem";

  return (
    <div
      data-testid="roadmap"
      className="h-full overflow-auto rounded-lg border border-border bg-card"
    >
      <div
        className="min-w-max"
        style={{
          display: "grid",
          gridTemplateColumns: `${laneWidth} repeat(${roadmap.columns.length}, ${columnWidth})`,
        }}
      >
        {/* Milestone track */}
        <div className="sticky left-0 z-10 border-b border-r border-border bg-card px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Milestones
        </div>
        {roadmap.columns.map((column) => {
          const pins = roadmap.milestones.filter((m) => m.columnId === column.id);
          return (
            <div
              key={`ms-${column.id}`}
              className="flex flex-wrap items-center gap-1 border-b border-r border-border px-2 py-2"
            >
              {pins.map((m) => (
                <span
                  key={m.id}
                  data-testid={`milestone-${m.id}`}
                  title={m.dueAt ?? m.label}
                  className="inline-flex max-w-full items-center gap-1 rounded-full border border-warning/50 bg-warning/10 px-2 py-0.5 text-[11px] text-warning-foreground"
                >
                  <Flag aria-hidden className="h-3 w-3 text-warning" />
                  <span className="truncate">{m.label}</span>
                </span>
              ))}
            </div>
          );
        })}

        {/* Sprint header row */}
        <div className="sticky left-0 z-10 border-b border-r border-border bg-card px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Epic
        </div>
        {roadmap.columns.map((column) => (
          <div
            key={`hd-${column.id}`}
            className={cn(
              "border-b border-r border-border px-3 py-2",
              column.unscheduled && "bg-muted/40",
            )}
          >
            <p className="font-display text-sm font-semibold tracking-tight">
              {column.label}
            </p>
            {formatRange(column) ? (
              <p className="font-mono text-[11px] text-muted-foreground">
                {formatRange(column)}
              </p>
            ) : null}
          </div>
        ))}

        {/* Epic swimlanes */}
        {roadmap.lanes.map((lane) => (
          <RoadmapLaneRow
            key={lane.id}
            laneId={lane.id}
            label={lane.label}
            columns={roadmap.columns}
            cells={roadmap.cell[lane.id]}
          />
        ))}
      </div>
    </div>
  );
}

interface RoadmapLaneRowProps {
  laneId: string;
  label: string;
  columns: RoadmapColumn[];
  cells: Record<string, TaskDTO[]>;
}

function RoadmapLaneRow({ laneId, label, columns, cells }: RoadmapLaneRowProps) {
  return (
    <>
      <div
        data-testid={`lane-${laneId}`}
        className="sticky left-0 z-10 flex items-center border-b border-r border-border bg-card px-3 py-3 text-sm font-medium"
      >
        <span className="truncate">{label}</span>
      </div>
      {columns.map((column) => {
        const cellTasks = cells[column.id] ?? [];
        return (
          <div
            key={`${laneId}-${column.id}`}
            data-testid={`cell-${laneId}-${column.id}`}
            className={cn(
              "flex flex-col gap-1 border-b border-r border-border p-2",
              column.unscheduled && "bg-muted/20",
            )}
          >
            {cellTasks.map((task) => (
              <div
                key={task.id ?? task.title}
                className="flex items-center gap-2 rounded-md border border-border bg-background px-2 py-1"
              >
                <span
                  aria-hidden
                  className={cn(
                    "h-2 w-2 shrink-0 rounded-full",
                    STATUS_DOT[(task.status as TaskStatus) ?? "backlog"],
                  )}
                  title={STATUS_LABELS[(task.status as TaskStatus) ?? "backlog"]}
                />
                <span className="truncate text-xs">{task.title}</span>
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}
