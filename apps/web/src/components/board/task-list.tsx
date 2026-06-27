"use client";

import { useCallback, useState, type KeyboardEvent } from "react";

import type { TaskDTO } from "@/lib/api/types";
import {
  PRIORITY_LABELS,
  STATUS_LABELS,
  statusOf,
} from "@/lib/board/status";
import { cn } from "@/lib/utils";

export interface TaskListProps {
  tasks: TaskDTO[];
  /** Called when a row is activated (click or Enter). */
  onSelect?: (task: TaskDTO) => void;
}

/**
 * Keyboard-first list view.
 *
 * The wrapper is focusable and owns selection: ArrowDown/`j` and ArrowUp/`k`
 * move the highlight; Enter activates the selected row (spec: "Full keyboard
 * navigation, no mouse required").
 */
export function TaskList({ tasks, onSelect }: TaskListProps) {
  const [selected, setSelected] = useState(-1);

  const move = useCallback(
    (delta: number) => {
      setSelected((current) => {
        if (tasks.length === 0) {
          return -1;
        }
        const start = current < 0 ? (delta > 0 ? -1 : 0) : current;
        const next = Math.min(Math.max(start + delta, 0), tasks.length - 1);
        return next;
      });
    },
    [tasks.length],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      switch (event.key) {
        case "ArrowDown":
        case "j":
          event.preventDefault();
          move(1);
          break;
        case "ArrowUp":
        case "k":
          event.preventDefault();
          move(-1);
          break;
        case "Enter":
          if (selected >= 0 && tasks[selected]) {
            event.preventDefault();
            onSelect?.(tasks[selected]);
          }
          break;
        default:
          break;
      }
    },
    [move, onSelect, selected, tasks],
  );

  if (tasks.length === 0) {
    return (
      <div
        data-testid="task-list"
        className="rounded-md border border-dashed border-border p-10 text-center text-sm text-muted-foreground"
      >
        No tasks yet. Press <kbd className="font-mono">⌘K</kbd> to create one.
      </div>
    );
  }

  return (
    <div
      data-testid="task-list"
      role="grid"
      aria-label="Tasks"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="overflow-hidden rounded-md border border-border outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <table className="w-full border-collapse text-sm">
        <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Key
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Title
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Status
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Priority
            </th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task, index) => {
            const isSelected = index === selected;
            return (
              <tr
                key={task.id ?? index}
                data-testid={`task-row-${task.id ?? index}`}
                aria-selected={isSelected}
                onClick={() => {
                  setSelected(index);
                  onSelect?.(task);
                }}
                className={cn(
                  "cursor-pointer border-t border-border transition-colors hover:bg-accent/50",
                  isSelected && "bg-accent text-accent-foreground",
                )}
              >
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {task.key ?? "—"}
                </td>
                <td className="px-3 py-2 font-medium">{task.title}</td>
                <td className="px-3 py-2">
                  <span className="inline-flex items-center rounded-full border border-border px-2 py-0.5 text-xs">
                    {STATUS_LABELS[statusOf(task)]}
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {task.priority ? PRIORITY_LABELS[task.priority] : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
