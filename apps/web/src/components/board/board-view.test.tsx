import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { TaskDTO } from "@/lib/api/types";

import { BoardView } from "./board-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const tasks: TaskDTO[] = [
  { id: "t1", key: "FORGE-1", title: "Build login", status: "backlog" },
  { id: "t2", key: "FORGE-2", title: "Add logout", status: "in_progress" },
];

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listTasks: vi.fn(() => Promise.resolve(tasks)),
    setTaskStatus: vi.fn((taskId: string, status: string) =>
      Promise.resolve({ id: taskId, title: "x", status } as TaskDTO),
    ),
    createTask: vi.fn((task: TaskDTO) => Promise.resolve(task)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderBoard(client: ForgeApiClient, initialView: "list" | "board" = "list") {
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
    <BoardView client={client} initialView={initialView} enableRealtime={false} />,
    { wrapper: Wrapper },
  );
}

describe("BoardView", () => {
  it("renders fetched tasks in the list view", async () => {
    renderBoard(makeClient());
    expect(await screen.findByText("Build login")).toBeInTheDocument();
    expect(screen.getByText("Add logout")).toBeInTheDocument();
  });

  it("switches to the kanban view via the toggle", async () => {
    renderBoard(makeClient());
    await screen.findByText("Build login");

    fireEvent.click(screen.getByRole("tab", { name: /board/i }));

    const inProgress = await screen.findByTestId("column-in_progress");
    expect(within(inProgress).getByText("Add logout")).toBeInTheDocument();
  });

  it("calls the API to change status when a card is moved", async () => {
    const client = makeClient();
    renderBoard(client, "board");
    await screen.findByTestId("card-t1");

    fireEvent.click(
      within(screen.getByTestId("card-t1")).getByRole("button", {
        name: /move forward/i,
      }),
    );

    await waitFor(() =>
      expect(client.setTaskStatus).toHaveBeenCalledWith("t1", "ready"),
    );
  });
});
