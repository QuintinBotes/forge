import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type {
  BurndownSeries,
  Sprint,
  TaskDTO,
  VelocityDashboard,
} from "@/lib/api/types";

import { SprintsView } from "./sprints-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const ACTIVE: Sprint = {
  id: "s-active",
  project_id: "default",
  workspace_id: "ws",
  name: "Sprint 7",
  goal: "Ship the forge",
  state: "active",
  start_date: "2026-06-01",
  end_date: "2026-06-14",
  started_at: "2026-06-01T00:00:00Z",
  completed_at: null,
  capacity_points: 40,
  committed_points: 34,
  committed_task_count: 8,
  completed_points: 20,
  added_points: 5,
  removed_points: 2,
  carryover_points: 0,
  remaining_points: 14,
  predictability: 0.59,
  scope_change_ratio: 0.2,
  velocity_version: 3,
};

const PLANNED: Sprint = {
  ...ACTIVE,
  id: "s-planned",
  name: "Sprint 8",
  goal: null,
  state: "planned",
  start_date: "2026-06-15",
  end_date: "2026-06-28",
  started_at: null,
  committed_points: 0,
  committed_task_count: 0,
  completed_points: 0,
  remaining_points: 0,
  predictability: 0,
  scope_change_ratio: 0,
};

const VELOCITY: VelocityDashboard = {
  project_id: "default",
  sprints: [
    {
      sprint_id: "s-5",
      name: "Sprint 5",
      end_date: "2026-05-03",
      committed_points: 30,
      completed_points: 28,
      predictability: 0.93,
    },
    {
      sprint_id: "s-6",
      name: "Sprint 6",
      end_date: "2026-05-17",
      committed_points: 32,
      completed_points: 25,
      predictability: 0.78,
    },
  ],
  summary: {
    sprint_count: 2,
    average_velocity: 26.5,
    rolling_3_velocity: 26.5,
    predictability_avg: 0.85,
    scope_change_avg: 0.1,
    forecast_low: 22,
    forecast_avg: 26.5,
    forecast_high: 31,
  },
};

const BURNDOWN: BurndownSeries = {
  sprint_id: "s-active",
  start_date: "2026-06-01",
  end_date: "2026-06-14",
  committed_points: 34,
  points: [
    {
      snapshot_date: "2026-06-01",
      scope_points: 34,
      remaining_points: 34,
      completed_points: 0,
      ideal_points: 34,
      completed_task_count: 0,
      remaining_task_count: 8,
    },
    {
      snapshot_date: "2026-06-02",
      scope_points: 34,
      remaining_points: 27,
      completed_points: 7,
      ideal_points: 28,
      completed_task_count: 1,
      remaining_task_count: 7,
    },
    {
      snapshot_date: "2026-06-03",
      scope_points: 34,
      remaining_points: 18,
      completed_points: 16,
      ideal_points: 22,
      completed_task_count: 3,
      remaining_task_count: 5,
    },
  ],
};

const TASKS: TaskDTO[] = [
  {
    id: "t-1",
    key: "FORGE-1",
    title: "Wire the bellows",
    status: "in_progress",
    priority: "high",
    sprint_id: "s-active",
  },
  {
    id: "t-2",
    key: "FORGE-2",
    title: "Temper the blade",
    status: "backlog",
    priority: "medium",
    sprint_id: "s-active",
  },
];

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listProjectSprints: vi.fn(() => Promise.resolve([ACTIVE, PLANNED])),
    getVelocityDashboard: vi.fn(() => Promise.resolve(VELOCITY)),
    getSprintBurndown: vi.fn(() => Promise.resolve(BURNDOWN)),
    listTasks: vi.fn(() => Promise.resolve(TASKS)),
    setTaskStatus: vi.fn((taskId: string, status: string) =>
      Promise.resolve({ ...TASKS[0], id: taskId, status }),
    ),
    startSprint: vi.fn(() => Promise.resolve({ ...PLANNED, state: "active" })),
    completeSprint: vi.fn(() =>
      Promise.resolve({
        sprint: { ...ACTIVE, state: "completed" },
        velocity: {
          committed_points: 34,
          completed_points: 20,
          added_points: 5,
          removed_points: 2,
          carryover_points: 14,
          committed_task_count: 8,
          completed_task_count: 5,
          carryover_task_count: 3,
          predictability: 0.59,
          scope_change_ratio: 0.2,
        },
        completed: [],
        carryover: [],
        added: [],
        removed: [],
      }),
    ),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(<SprintsView client={client} />, { wrapper: Wrapper });
}

describe("SprintsView", () => {
  it("focuses the running sprint and renders its KPIs, board and charts", async () => {
    renderView(makeClient());

    // Active sprint selected by default; its committed + predictability show.
    expect(await screen.findByTestId("kpi-committed")).toHaveTextContent("34 pts");
    expect(screen.getByTestId("kpi-predictability")).toHaveTextContent("59%");

    // The board renders the sprint's tasks.
    expect(await screen.findByTestId("card-t-1")).toHaveTextContent("Wire the bellows");

    // Both charts land.
    expect(await screen.findByTestId("velocity-chart")).toBeInTheDocument();
    expect(await screen.findByTestId("burndown-chart")).toBeInTheDocument();

    // The one ember action reflects the active lifecycle (complete).
    expect(screen.getByTestId("lifecycle-action")).toHaveTextContent("Complete sprint");
  });

  it("moves a task across the board via the optimistic status mutation", async () => {
    const client = makeClient();
    renderView(client);

    const card = await screen.findByTestId("card-t-1"); // in_progress
    fireEvent.click(within(card).getByRole("button", { name: "Move forward" }));

    await waitFor(() =>
      expect(client.setTaskStatus).toHaveBeenCalledWith("t-1", "in_review"),
    );
  });

  it("switches the focused sprint and re-queries its burndown", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("kpi-committed");

    fireEvent.change(screen.getByTestId("sprint-picker"), {
      target: { value: "s-planned" },
    });

    // Lifecycle action flips to Start for the planned sprint.
    await waitFor(() =>
      expect(screen.getByTestId("lifecycle-action")).toHaveTextContent("Start sprint"),
    );
    await waitFor(() =>
      expect(client.getSprintBurndown).toHaveBeenCalledWith("s-planned"),
    );
  });

  it("starts a planned sprint and announces it", async () => {
    const client = makeClient({
      listProjectSprints: vi.fn(() => Promise.resolve([PLANNED])),
    });
    renderView(client);

    const action = await screen.findByTestId("lifecycle-action");
    expect(action).toHaveTextContent("Start sprint");
    fireEvent.click(action);

    await waitFor(() =>
      expect(client.startSprint).toHaveBeenCalledWith("s-planned"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("sprints-status")).toHaveTextContent(
        "Started Sprint 8.",
      ),
    );
  });

  it("completes an active sprint, routing carryover to the backlog", async () => {
    const client = makeClient();
    renderView(client);

    const action = await screen.findByTestId("lifecycle-action");
    fireEvent.click(action);

    await waitFor(() =>
      expect(client.completeSprint).toHaveBeenCalledWith("s-active", {
        carryover: "backlog",
      }),
    );
  });

  it("shows a loading skeleton while sprints are in flight", () => {
    const client = makeClient({
      listProjectSprints: vi.fn(() => new Promise<Sprint[]>(() => {})),
    });
    renderView(client);
    expect(screen.getByTestId("sprints-skeleton")).toBeInTheDocument();
  });

  it("degrades to an error state when the sprint service fails", async () => {
    const client = makeClient({
      listProjectSprints: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(client);
    expect(await screen.findByTestId("sprints-error")).toBeInTheDocument();
  });

  it("guides the user to plan a sprint when there are none", async () => {
    const client = makeClient({
      listProjectSprints: vi.fn(() => Promise.resolve([])),
    });
    renderView(client);
    expect(await screen.findByTestId("sprints-empty")).toBeInTheDocument();
  });

  it("shows an empty velocity state before any sprint is completed", async () => {
    const client = makeClient({
      getVelocityDashboard: vi.fn(() =>
        Promise.resolve({ ...VELOCITY, sprints: [] }),
      ),
    });
    renderView(client);
    expect(await screen.findByTestId("empty-velocity")).toBeInTheDocument();
  });

  it("shows an empty burndown state when no snapshots exist yet", async () => {
    const client = makeClient({
      getSprintBurndown: vi.fn(() =>
        Promise.resolve({ ...BURNDOWN, points: [] }),
      ),
    });
    renderView(client);
    expect(await screen.findByTestId("empty-burndown")).toBeInTheDocument();
  });
});
