import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { Principal, TaskDTO } from "@/lib/api/types";

import { BoardDepth, type DepthViewMode } from "./board-depth";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const tasks: TaskDTO[] = [
  { id: "t1", key: "FORGE-1", title: "Build login", status: "backlog", priority: "high" },
  { id: "t2", key: "FORGE-2", title: "Add logout", status: "backlog", priority: "low" },
  { id: "t3", key: "FORGE-3", title: "Fix crash", status: "blocked", priority: "urgent" },
];

const principal: Principal = { user_id: "u1", workspace_id: "w1", role: "admin" };

interface ClientOverrides {
  listTasks?: ForgeApiClient["listTasks"];
}

function makeClient(overrides: ClientOverrides = {}): ForgeApiClient {
  return {
    listTasks: overrides.listTasks ?? vi.fn(() => Promise.resolve(tasks)),
    listEpics: vi.fn(() => Promise.resolve([])),
    listSprints: vi.fn(() => Promise.resolve([])),
    listMilestones: vi.fn(() => Promise.resolve([])),
    me: vi.fn(() => Promise.resolve(principal)),
    setTaskStatus: vi.fn((taskId: string, status: string) =>
      Promise.resolve({ id: taskId, title: "x", status } as TaskDTO),
    ),
    bulkUpdateTasks: vi.fn((updates) => Promise.resolve(updates as TaskDTO[])),
    createTask: vi.fn((task: TaskDTO) => Promise.resolve(task)),
    updateTask: vi.fn((taskId: string) => Promise.resolve({ id: taskId, title: "x" } as TaskDTO)),
  } as unknown as ForgeApiClient;
}

function renderDepth(client: ForgeApiClient, initialView: DepthViewMode = "board") {
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
  return render(
    <BoardDepth client={client} initialView={initialView} enableRealtime={false} />,
    { wrapper: Wrapper },
  );
}

describe("BoardDepth", () => {
  it("shows a loading state while tasks are pending", () => {
    const client = makeClient({ listTasks: vi.fn(() => new Promise<TaskDTO[]>(() => {})) });
    renderDepth(client);
    expect(screen.getByText(/loading board/i)).toBeInTheDocument();
  });

  it("shows an error state when the board fails to load", async () => {
    const client = makeClient({ listTasks: vi.fn(() => Promise.reject(new Error("boom"))) });
    renderDepth(client);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t load the board/i);
  });

  it("shows an empty state when there are no tasks", async () => {
    const client = makeClient({ listTasks: vi.fn(() => Promise.resolve([])) });
    renderDepth(client);
    expect(await screen.findByText(/no tasks yet/i)).toBeInTheDocument();
  });

  it("renders tasks on the Kanban", async () => {
    renderDepth(makeClient());
    expect(await screen.findByTestId("card-t1")).toBeInTheDocument();
    expect(screen.getByText("Add logout")).toBeInTheDocument();
  });

  it("switches to the roadmap view", async () => {
    renderDepth(makeClient());
    await screen.findByTestId("card-t1");
    fireEvent.click(screen.getByRole("tab", { name: /roadmap/i }));
    expect(await screen.findByTestId("roadmap")).toBeInTheDocument();
  });

  it("filters by the search box", async () => {
    renderDepth(makeClient());
    await screen.findByTestId("card-t1");
    fireEvent.change(screen.getByLabelText(/search tasks/i), {
      target: { value: "login" },
    });
    expect(screen.getByText("Build login")).toBeInTheDocument();
    expect(screen.queryByText("Add logout")).not.toBeInTheDocument();
  });

  it("filters by a preset view chip", async () => {
    renderDepth(makeClient());
    await screen.findByTestId("card-t1");
    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(screen.getByText("Fix crash")).toBeInTheDocument();
    expect(screen.queryByText("Build login")).not.toBeInTheDocument();
  });

  it("selects cards and applies a bulk status change via the API", async () => {
    const client = makeClient();
    renderDepth(client);
    await screen.findByTestId("card-t1");

    fireEvent.click(screen.getByLabelText("Select Build login"));
    const bar = await screen.findByRole("region", { name: /bulk actions/i });
    expect(bar).toHaveTextContent("1 selected");

    fireEvent.change(within(bar).getByLabelText(/set status for selected/i), {
      target: { value: "ready" },
    });
    await waitFor(() =>
      expect(client.bulkUpdateTasks).toHaveBeenCalledWith([
        { task_id: "t1", status: "ready" },
      ]),
    );
  });

  it("opens the create-task dialog from the toolbar", async () => {
    renderDepth(makeClient());
    await screen.findByTestId("card-t1");
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    expect(await screen.findByText(/add a new task to the board/i)).toBeInTheDocument();
  });

  it("exposes deep palette commands scoped to the selection", async () => {
    renderDepth(makeClient());
    await screen.findByTestId("card-t1");

    fireEvent.click(screen.getByLabelText("Select Build login"));
    fireEvent.keyDown(document, { key: "k", metaKey: true });

    expect(await screen.findByText("View: Roadmap")).toBeInTheDocument();
    expect(screen.getByText("Set status: Done")).toBeInTheDocument();
    // Resolves once the principal (client.me) loads → "assign" becomes available.
    expect(await screen.findByText("Assign selected to me")).toBeInTheDocument();
  });
});
