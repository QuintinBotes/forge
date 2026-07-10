"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import type { TaskDTO, TaskStatus } from "@/lib/api/types";
import {
  PRIORITY_LABELS,
  STATUS_COLUMNS,
  STATUS_LABELS,
  groupByStatus,
  nextStatus,
  prevStatus,
  statusOf,
} from "@/lib/board/status";
import { cn } from "@/lib/utils";

export interface KanbanBoardProps {
  tasks: TaskDTO[];
  /** Fired when a card is moved to an adjacent column. */
  onStatusChange?: (taskId: string, status: TaskStatus) => void;
  onSelect?: (task: TaskDTO) => void;
  /** Restrict the visible columns (defaults to the full workflow order). */
  columns?: readonly TaskStatus[];
}

/**
 * Kanban view. One column per status; each card exposes forward/back controls
 * that move it to the adjacent column via {@link KanbanBoardProps.onStatusChange}
 * (wired to the optimistic status mutation by the board page).
 */
export function KanbanBoard({
  tasks,
  onStatusChange,
  onSelect,
  columns = STATUS_COLUMNS,
}: KanbanBoardProps) {
  const grouped = groupByStatus(tasks);

  return (
    <div className="flex h-full gap-3 overflow-x-auto pb-2">
      {columns.map((status) => {
        const columnTasks = grouped[status];
        return (
          <section
            key={status}
            data-testid={`column-${status}`}
            aria-label={STATUS_LABELS[status]}
            className="flex w-72 shrink-0 flex-col rounded-md border border-border bg-card"
          >
            <header className="flex items-center justify-between border-b border-border px-3 py-2">
              <h2 className="text-sm font-medium">{STATUS_LABELS[status]}</h2>
              <span
                data-testid="column-count"
                className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
              >
                {columnTasks.length}
              </span>
            </header>
            <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-2">
              {columnTasks.length === 0 ? (
                <p
                  data-testid={`column-empty-${status}`}
                  className="rounded-md border border-dashed border-border px-2 py-4 text-center text-xs text-muted-foreground"
                >
                  No tasks
                </p>
              ) : (
                columnTasks.map((task) => (
                  <KanbanCard
                    key={task.id}
                    task={task}
                    onStatusChange={onStatusChange}
                    onSelect={onSelect}
                  />
                ))
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}

interface KanbanCardProps {
  task: TaskDTO;
  onStatusChange?: (taskId: string, status: TaskStatus) => void;
  onSelect?: (task: TaskDTO) => void;
}

function KanbanCard({ task, onStatusChange, onSelect }: KanbanCardProps) {
  const status = statusOf(task);
  const back = prevStatus(status);
  const forward = nextStatus(status);
  const taskId = task.id;

  return (
    <article
      data-testid={`card-${taskId ?? task.title}`}
      className="rounded-md border border-border bg-background p-2 shadow-sm"
    >
      <button
        type="button"
        onClick={() => onSelect?.(task)}
        className="block w-full rounded text-left text-sm font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {task.title}
      </button>
      <div className="mt-2 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {task.key ? <span className="font-mono">{task.key}</span> : null}
          {task.priority ? <span>{PRIORITY_LABELS[task.priority]}</span> : null}
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Move back"
            disabled={!back || !taskId}
            onClick={() => back && taskId && onStatusChange?.(taskId, back)}
            className={cn(
              "rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              (!back || !taskId) && "cursor-not-allowed opacity-40",
            )}
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            type="button"
            aria-label="Move forward"
            disabled={!forward || !taskId}
            onClick={() => forward && taskId && onStatusChange?.(taskId, forward)}
            className={cn(
              "rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              (!forward || !taskId) && "cursor-not-allowed opacity-40",
            )}
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </article>
  );
}
