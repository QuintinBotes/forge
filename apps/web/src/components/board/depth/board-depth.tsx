"use client";

import {
  CalendarRange,
  CircleDot,
  Inbox,
  KanbanSquare,
  LayoutList,
  Loader2,
  Plus,
  Search,
  UserPlus,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  useRegisterCommands,
  type CommandAction,
} from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useBulkUpdateTasks,
  useCreateTask,
  useCurrentUser,
  useEpics,
  useMilestones,
  useSetTaskStatus,
  useSprints,
  useTasks,
} from "@/lib/api/hooks";
import type { BulkUpdate, TaskDTO, TaskStatus } from "@/lib/api/types";
import {
  PRESET_VIEWS,
  filterTasks,
  loadSavedViews,
  persistSavedViews,
  type BoardView,
} from "@/lib/board/filters";
import { STATUS_LABELS, canTransition, statusOf } from "@/lib/board/status";
import { useBoardRealtime } from "@/lib/realtime/use-board-realtime";
import { cn } from "@/lib/utils";

import { NewTaskDialog, toCreatePayload, type NewTaskInput } from "../new-task-dialog";
import { TaskList } from "../task-list";
import { BulkActionBar } from "./bulk-action-bar";
import { DepthKanban } from "./depth-kanban";
import { DepthRoadmap } from "./depth-roadmap";
import { SavedFiltersBar } from "./saved-filters-bar";

export type DepthViewMode = "board" | "roadmap" | "list";

export interface BoardDepthProps {
  initialView?: DepthViewMode;
  client?: ForgeApiClient;
  enableRealtime?: boolean;
}

/** Statuses surfaced as one-tap bulk targets in the command palette. */
const PALETTE_STATUS_TARGETS: TaskStatus[] = [
  "in_progress",
  "in_review",
  "blocked",
  "done",
];

/**
 * "Board depth": the full board surface — status-rule-aware Kanban, a roadmap
 * timeline, a saved-filters bar, multi-select bulk actions, and a deep Cmd+K
 * palette (create / status / assign / navigate / search). All wired to the typed
 * board API with optimistic mutations and realtime cache invalidation.
 */
export function BoardDepth({
  initialView = "board",
  client = apiClient,
  enableRealtime = true,
}: BoardDepthProps) {
  const [view, setView] = useState<DepthViewMode>(initialView);
  const [activeView, setActiveView] = useState<BoardView>(PRESET_VIEWS[0]);
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<ReadonlySet<string>>(new Set());
  const [savedViews, setSavedViews] = useState<BoardView[]>(() => loadSavedViews());
  const [createOpen, setCreateOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  // Focus is requested via a state "tick" (a stable setter is safe to call from
  // a render-phase command builder); the effect — allowed to read refs — moves
  // focus to the search box when the tick advances.
  const [focusTick, setFocusTick] = useState(0);
  useEffect(() => {
    if (focusTick > 0) {
      searchRef.current?.focus();
    }
  }, [focusTick]);

  const tasksQuery = useTasks(undefined, client);
  const epicsQuery = useEpics(client);
  const sprintsQuery = useSprints(client);
  const milestonesQuery = useMilestones(client);
  const currentUser = useCurrentUser(client);

  const setStatus = useSetTaskStatus(client);
  const bulkUpdate = useBulkUpdateTasks(client);
  const createTask = useCreateTask(client);

  // Mutation *result* objects are recreated each render, but their `mutate`
  // functions are referentially stable. Depending on those (not the objects)
  // keeps the memoised handlers — and thus the registered Cmd+K command set —
  // from thrashing / re-registering on every render.
  const { mutate: setStatusMutate } = setStatus;
  const { mutate: bulkMutate } = bulkUpdate;
  const { mutate: createMutate } = createTask;

  useBoardRealtime({ enabled: enableRealtime });

  const currentUserId = currentUser.data?.user_id ?? null;
  const allTasks: TaskDTO[] = useMemo(() => tasksQuery.data ?? [], [tasksQuery.data]);
  const taskById = useMemo(() => {
    const map = new Map<string, TaskDTO>();
    for (const task of allTasks) {
      if (task.id) {
        map.set(task.id, task);
      }
    }
    return map;
  }, [allTasks]);

  const visibleTasks = useMemo(
    () => filterTasks(allTasks, { view: activeView, query, currentUserId }),
    [allTasks, activeView, query, currentUserId],
  );

  const views = useMemo(() => [...PRESET_VIEWS, ...savedViews], [savedViews]);
  const removableIds = useMemo(
    () => new Set(savedViews.map((v) => v.id)),
    [savedViews],
  );

  // --- Selection -------------------------------------------------------- //
  const toggleSelect = useCallback((taskId: string) => {
    setSelection((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => setSelection(new Set()), []);

  // --- Mutations -------------------------------------------------------- //
  const handleSingleStatus = useCallback(
    (taskId: string, status: TaskStatus) => {
      setStatusMutate({ taskId, status });
    },
    [setStatusMutate],
  );

  const handleBulkStatus = useCallback(
    (taskIds: string[], status: TaskStatus) => {
      const legal = taskIds.filter((id) => {
        const task = taskById.get(id);
        return (
          task != null &&
          statusOf(task) !== status &&
          canTransition(statusOf(task), status)
        );
      });
      if (legal.length === 0) {
        return;
      }
      const updates: BulkUpdate[] = legal.map((id) => ({ task_id: id, status }));
      bulkMutate(updates);
    },
    [bulkMutate, taskById],
  );

  const handleAssignToMe = useCallback(() => {
    if (!currentUserId || selection.size === 0) {
      return;
    }
    const updates: BulkUpdate[] = [...selection].map((id) => ({
      task_id: id,
      assignee_id: currentUserId,
    }));
    bulkMutate(updates);
  }, [bulkMutate, currentUserId, selection]);

  const handleCreate = useCallback(
    (input: NewTaskInput) => {
      createMutate(toCreatePayload(input), {
        onSuccess: () => setCreateOpen(false),
      });
    },
    [createMutate],
  );

  // --- Saved views ------------------------------------------------------ //
  const selectView = useCallback((next: BoardView) => {
    setActiveView(next);
    setQuery(next.query ?? "");
  }, []);

  const saveCurrentView = useCallback(
    (label: string) => {
      const snapshot: BoardView = {
        id: `saved-${Date.now().toString(36)}`,
        label,
        statuses: activeView.statuses,
        priorities: activeView.priorities,
        assignee: activeView.assignee,
        query: query.trim() || undefined,
      };
      setSavedViews((prev) => {
        const next = [...prev, snapshot];
        persistSavedViews(next);
        return next;
      });
      setActiveView(snapshot);
    },
    [activeView, query],
  );

  const deleteSavedView = useCallback(
    (id: string) => {
      setSavedViews((prev) => {
        const next = prev.filter((v) => v.id !== id);
        persistSavedViews(next);
        return next;
      });
      setActiveView((prev) => (prev.id === id ? PRESET_VIEWS[0] : prev));
    },
    [],
  );

  // --- Cmd+K palette (create / status / assign / navigate / search) ----- //
  const commands = useMemo<CommandAction[]>(() => {
    const list: CommandAction[] = [
      {
        id: "depth-create-task",
        label: "Create task",
        group: "Create",
        icon: <Plus />,
        shortcut: "C",
        run: () => setCreateOpen(true),
      },
      {
        id: "depth-view-board",
        label: "View: Board",
        group: "Navigate",
        icon: <KanbanSquare />,
        run: () => setView("board"),
      },
      {
        id: "depth-view-roadmap",
        label: "View: Roadmap",
        group: "Navigate",
        icon: <CalendarRange />,
        run: () => setView("roadmap"),
      },
      {
        id: "depth-view-list",
        label: "View: List",
        group: "Navigate",
        icon: <LayoutList />,
        run: () => setView("list"),
      },
      {
        id: "depth-search",
        label: "Search tasks",
        group: "Search",
        icon: <Search />,
        shortcut: "/",
        run: () => setFocusTick((tick) => tick + 1),
      },
    ];

    if (selection.size > 0) {
      const ids = [...selection];
      for (const status of PALETTE_STATUS_TARGETS) {
        list.push({
          id: `depth-status-${status}`,
          label: `Set status: ${STATUS_LABELS[status]}`,
          group: "Selection",
          icon: <CircleDot />,
          run: () => handleBulkStatus(ids, status),
        });
      }
      if (currentUserId) {
        list.push({
          id: "depth-assign-me",
          label: "Assign selected to me",
          group: "Selection",
          icon: <UserPlus />,
          run: () => handleAssignToMe(),
        });
      }
    }
    return list;
  }, [selection, currentUserId, handleBulkStatus, handleAssignToMe]);

  useRegisterCommands("board-depth", commands);

  // --- Render ----------------------------------------------------------- //
  const isLoading = tasksQuery.isPending;
  const isError = tasksQuery.isError;

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h1 className="font-display text-xl font-semibold tracking-tight">Board</h1>
          <div
            role="tablist"
            aria-label="Board view"
            className="inline-flex rounded-md border border-border p-0.5"
          >
            <ViewTab
              active={view === "board"}
              label="Board"
              icon={<KanbanSquare className="h-4 w-4" />}
              onClick={() => setView("board")}
            />
            <ViewTab
              active={view === "roadmap"}
              label="Roadmap"
              icon={<CalendarRange className="h-4 w-4" />}
              onClick={() => setView("roadmap")}
            />
            <ViewTab
              active={view === "list"}
              label="List"
              icon={<LayoutList className="h-4 w-4" />}
              onClick={() => setView("list")}
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search
              aria-hidden
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            />
            <input
              ref={searchRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search tasks…"
              aria-label="Search tasks"
              className="h-9 w-56 rounded-md border border-input bg-background pl-8 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            New task
          </Button>
        </div>
      </div>

      <SavedFiltersBar
        views={views}
        activeId={activeView.id}
        removableIds={removableIds}
        onSelect={selectView}
        onDelete={deleteSavedView}
        onSaveCurrent={saveCurrentView}
        canSave={activeView.id !== "all" || query.trim().length > 0}
      />

      <BulkActionBar
        count={selection.size}
        onSetStatus={(status) => handleBulkStatus([...selection], status)}
        onAssignToMe={handleAssignToMe}
        canAssign={Boolean(currentUserId)}
        onClear={clearSelection}
        pending={bulkUpdate.isPending}
      />

      <div className="min-h-0 flex-1">
        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState onRetry={() => void tasksQuery.refetch()} />
        ) : allTasks.length === 0 ? (
          <EmptyState onCreate={() => setCreateOpen(true)} />
        ) : view === "roadmap" ? (
          <DepthRoadmap
            tasks={visibleTasks}
            epics={epicsQuery.data ?? []}
            sprints={sprintsQuery.data ?? []}
            milestones={milestonesQuery.data ?? []}
          />
        ) : visibleTasks.length === 0 ? (
          <NoMatchState
            onClear={() => {
              setActiveView(PRESET_VIEWS[0]);
              setQuery("");
            }}
          />
        ) : view === "board" ? (
          <DepthKanban
            tasks={visibleTasks}
            selection={selection}
            onToggleSelect={toggleSelect}
            onStatusChange={handleSingleStatus}
            onBulkStatus={handleBulkStatus}
          />
        ) : (
          <TaskList tasks={visibleTasks} />
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

function LoadingState() {
  return (
    <div
      role="status"
      aria-label="Loading board"
      className="flex h-full items-center justify-center gap-2 rounded-lg border border-dashed border-border text-sm text-muted-foreground"
    >
      <Loader2 className="h-4 w-4 animate-spin" />
      Loading board…
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-danger/40 bg-danger/5 p-10 text-center"
    >
      <p className="text-sm font-medium">Couldn’t load the board</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        The board service didn’t respond. Check your connection and try again.
      </p>
      <Button size="sm" variant="outline" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border p-12 text-center">
      <Inbox aria-hidden className="h-6 w-6 text-muted-foreground" />
      <p className="text-sm font-medium">No tasks yet</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        Create the first task to start moving work across the board. Press{" "}
        <kbd className="rounded border border-border px-1 font-mono text-[11px]">⌘K</kbd>{" "}
        any time.
      </p>
      <Button size="sm" onClick={onCreate}>
        <Plus className="h-4 w-4" />
        New task
      </Button>
    </div>
  );
}

function NoMatchState({ onClear }: { onClear: () => void }) {
  return (
    <div
      data-testid="no-match"
      className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border p-12 text-center"
    >
      <Search aria-hidden className="h-6 w-6 text-muted-foreground" />
      <p className="text-sm font-medium">No tasks match this view</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        Adjust the filters or search to see more work.
      </p>
      <Button size="sm" variant="outline" onClick={onClear}>
        Clear filters
      </Button>
    </div>
  );
}
