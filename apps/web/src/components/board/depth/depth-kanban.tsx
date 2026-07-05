"use client";

import { ChevronLeft, ChevronRight, GripVertical } from "lucide-react";
import { useState, type DragEvent } from "react";

import type { TaskDTO, TaskStatus } from "@/lib/api/types";
import {
  PRIORITY_LABELS,
  STATUS_COLUMNS,
  STATUS_LABELS,
  backwardTarget,
  canTransition,
  forwardTarget,
  groupByStatus,
  statusOf,
} from "@/lib/board/status";
import { cn } from "@/lib/utils";

export interface DepthKanbanProps {
  tasks: TaskDTO[];
  /** Currently multi-selected task ids. */
  selection: ReadonlySet<string>;
  onToggleSelect: (taskId: string) => void;
  /** Move a single card to `status` (already validated as legal). */
  onStatusChange: (taskId: string, status: TaskStatus) => void;
  /** Move many cards at once (drag of a selected card, or bulk bar). */
  onBulkStatus: (taskIds: string[], status: TaskStatus) => void;
  columns?: readonly TaskStatus[];
}

interface DragState {
  id: string;
  status: TaskStatus;
}

/**
 * The depth Kanban: drag-to-move with the workflow's transition rules baked into
 * the drop affordance. While a card is dragged, columns that are *legal* targets
 * warm up (spark ring); illegal columns dim and reject the drop. Cards keep
 * keyboard-reachable move buttons, so the board is fully operable without a mouse.
 */
export function DepthKanban({
  tasks,
  selection,
  onToggleSelect,
  onStatusChange,
  onBulkStatus,
  columns = STATUS_COLUMNS,
}: DepthKanbanProps) {
  const grouped = groupByStatus(tasks);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [overColumn, setOverColumn] = useState<TaskStatus | null>(null);

  const endDrag = () => {
    setDrag(null);
    setOverColumn(null);
  };

  const isLegalTarget = (column: TaskStatus): boolean =>
    drag != null && drag.status !== column && canTransition(drag.status, column);

  const handleDrop = (column: TaskStatus) => {
    if (!drag || !isLegalTarget(column)) {
      endDrag();
      return;
    }
    const draggingSelected = selection.has(drag.id) && selection.size > 1;
    if (draggingSelected) {
      const movable = tasks
        .filter(
          (t) =>
            t.id != null &&
            selection.has(t.id) &&
            canTransition(statusOf(t), column) &&
            statusOf(t) !== column,
        )
        .map((t) => t.id as string);
      if (movable.length > 0) {
        onBulkStatus(movable, column);
      }
    } else {
      onStatusChange(drag.id, column);
    }
    endDrag();
  };

  return (
    <div
      className="flex h-full gap-3 overflow-x-auto pb-2"
      role="list"
      aria-label="Board columns"
    >
      {columns.map((status) => {
        const columnTasks = grouped[status];
        const legal = isLegalTarget(status);
        const dimmed = drag != null && !legal && drag.status !== status;
        const isOver = overColumn === status && legal;
        return (
          <section
            key={status}
            role="listitem"
            data-testid={`column-${status}`}
            data-legal-drop={drag ? (legal ? "yes" : "no") : undefined}
            aria-label={STATUS_LABELS[status]}
            onDragOver={(event: DragEvent<HTMLElement>) => {
              if (legal) {
                event.preventDefault();
                setOverColumn(status);
              }
            }}
            onDragLeave={() => setOverColumn((c) => (c === status ? null : c))}
            onDrop={(event: DragEvent<HTMLElement>) => {
              event.preventDefault();
              handleDrop(status);
            }}
            className={cn(
              "flex w-72 shrink-0 flex-col rounded-lg border bg-card transition-all",
              "border-border",
              legal && "border-spark/60",
              isOver && "border-primary bg-accent/60 ring-2 ring-spark",
              dimmed && "opacity-45",
            )}
          >
            <header className="flex items-center justify-between border-b border-border px-3 py-2">
              <h2 className="font-display text-sm font-semibold tracking-tight">
                {STATUS_LABELS[status]}
              </h2>
              <span
                data-testid="column-count"
                className="rounded-full bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
              >
                {columnTasks.length}
              </span>
            </header>
            <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-2">
              {columnTasks.length === 0 ? (
                <p className="rounded-md border border-dashed border-border/70 px-2 py-6 text-center text-xs text-muted-foreground">
                  {legal ? "Drop here" : "Empty"}
                </p>
              ) : (
                columnTasks.map((task) => (
                  <KanbanCard
                    key={task.id ?? task.title}
                    task={task}
                    columns={columns}
                    selected={task.id != null && selection.has(task.id)}
                    onToggleSelect={onToggleSelect}
                    onStatusChange={onStatusChange}
                    onDragStart={(status2) =>
                      task.id != null && setDrag({ id: task.id, status: status2 })
                    }
                    onDragEnd={endDrag}
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
  columns: readonly TaskStatus[];
  selected: boolean;
  onToggleSelect: (taskId: string) => void;
  onStatusChange: (taskId: string, status: TaskStatus) => void;
  onDragStart: (status: TaskStatus) => void;
  onDragEnd: () => void;
}

function KanbanCard({
  task,
  columns,
  selected,
  onToggleSelect,
  onStatusChange,
  onDragStart,
  onDragEnd,
}: KanbanCardProps) {
  const status = statusOf(task);
  const taskId = task.id;
  const back = backwardTarget(status, columns);
  const forward = forwardTarget(status, columns);

  return (
    <article
      draggable={taskId != null}
      data-testid={`card-${taskId ?? task.title}`}
      onDragStart={(event: DragEvent<HTMLElement>) => {
        if (taskId == null) {
          return;
        }
        event.dataTransfer?.setData?.("text/plain", taskId);
        onDragStart(status);
      }}
      onDragEnd={onDragEnd}
      className={cn(
        "group rounded-md border bg-background p-2 shadow-sm transition-colors",
        selected ? "border-spark ring-2 ring-spark" : "border-border hover:border-primary/40",
      )}
    >
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={selected}
          aria-label={`Select ${task.title}`}
          onChange={() => taskId != null && onToggleSelect(taskId)}
          className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
        />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{task.title}</p>
          <div className="mt-2 flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
              {task.key ? <span className="font-mono">{task.key}</span> : null}
              {task.priority ? (
                <span className="rounded-sm bg-muted px-1.5 py-0.5">
                  {PRIORITY_LABELS[task.priority]}
                </span>
              ) : null}
            </div>
            <div className="flex items-center gap-0.5">
              <button
                type="button"
                aria-label="Move back"
                disabled={!back || !taskId}
                onClick={() => back && taskId && onStatusChange(taskId, back)}
                className={cn(
                  "rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  (!back || !taskId) && "cursor-not-allowed opacity-40",
                )}
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <button
                type="button"
                aria-label="Move forward"
                disabled={!forward || !taskId}
                onClick={() => forward && taskId && onStatusChange(taskId, forward)}
                className={cn(
                  "rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  (!forward || !taskId) && "cursor-not-allowed opacity-40",
                )}
              >
                <ChevronRight className="h-4 w-4" />
              </button>
              <GripVertical
                aria-hidden
                className="h-4 w-4 cursor-grab text-muted-foreground/50 group-hover:text-muted-foreground"
              />
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}
