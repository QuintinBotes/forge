"use client";

import { KanbanSquare, LayoutList, Plus } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { buildBoardCommands, useRegisterCommands } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useCreateTask,
  useSetTaskStatus,
  useTasks,
} from "@/lib/api/hooks";
import type { TaskDTO, TaskStatus } from "@/lib/api/types";
import { useBoardRealtime } from "@/lib/realtime/use-board-realtime";
import { cn } from "@/lib/utils";

import { KanbanBoard } from "./kanban-board";
import { NewTaskDialog, toCreatePayload, type NewTaskInput } from "./new-task-dialog";
import { TaskList } from "./task-list";

export type BoardViewMode = "list" | "board";

export interface BoardViewProps {
  initialView?: BoardViewMode;
  client?: ForgeApiClient;
  filters?: Record<string, string | number | boolean | undefined>;
  /** Disable the WebSocket subscription (e.g. in tests). */
  enableRealtime?: boolean;
}

/**
 * The board surface: List/Kanban toggle, optimistic status changes, realtime
 * cache invalidation, and command-palette "Create task" wiring.
 */
export function BoardView({
  initialView = "list",
  client = apiClient,
  filters,
  enableRealtime = true,
}: BoardViewProps) {
  const [view, setView] = useState<BoardViewMode>(initialView);
  const [createOpen, setCreateOpen] = useState(false);

  const tasksQuery = useTasks(filters, client);
  const setStatus = useSetTaskStatus(client);
  const createTask = useCreateTask(client);

  useBoardRealtime({ enabled: enableRealtime });

  const boardCommands = useMemo(
    () => buildBoardCommands({ onCreateTask: () => setCreateOpen(true) }),
    [],
  );
  useRegisterCommands("board", boardCommands);

  const handleStatusChange = useCallback(
    (taskId: string, status: TaskStatus) => {
      setStatus.mutate({ taskId, status });
    },
    [setStatus],
  );

  const handleCreate = useCallback(
    (input: NewTaskInput) => {
      createTask.mutate(toCreatePayload(input), {
        onSuccess: () => setCreateOpen(false),
      });
    },
    [createTask],
  );

  const tasks: TaskDTO[] = tasksQuery.data ?? [];

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-center justify-between">
        <div
          role="tablist"
          aria-label="Board view"
          className="inline-flex rounded-md border border-border p-0.5"
        >
          <ViewTab
            active={view === "list"}
            label="List"
            icon={<LayoutList className="h-4 w-4" />}
            onClick={() => setView("list")}
          />
          <ViewTab
            active={view === "board"}
            label="Board"
            icon={<KanbanSquare className="h-4 w-4" />}
            onClick={() => setView("board")}
          />
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          New task
        </Button>
      </div>

      {tasksQuery.isError ? (
        <p
          role="status"
          className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
        >
          Live board data is not available yet — showing an empty board.
        </p>
      ) : null}

      <div className="min-h-0 flex-1">
        {view === "list" ? (
          <TaskList tasks={tasks} />
        ) : (
          <KanbanBoard tasks={tasks} onStatusChange={handleStatusChange} />
        )}
      </div>

      <NewTaskDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreate={handleCreate}
        pending={createTask.isPending}
      />
    </div>
  );
}

interface ViewTabProps {
  active: boolean;
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}

function ViewTab({ active, label, icon, onClick }: ViewTabProps) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-2 rounded px-3 py-1.5 text-sm font-medium transition-colors",
        active
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {icon}
      {label}
    </button>
  );
}
