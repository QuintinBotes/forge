import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TaskDTO } from "@/lib/api/types";
import { KanbanBoard } from "./kanban-board";

const tasks: TaskDTO[] = [
  { id: "t1", key: "FORGE-1", title: "Build login", status: "backlog" },
  { id: "t2", key: "FORGE-2", title: "Add logout", status: "in_progress" },
  { id: "t3", key: "FORGE-3", title: "Fix crash", status: "in_progress" },
];

describe("KanbanBoard", () => {
  it("renders a column per status with the right cards and counts", () => {
    render(<KanbanBoard tasks={tasks} />);

    const backlog = screen.getByTestId("column-backlog");
    expect(within(backlog).getByText("Build login")).toBeInTheDocument();

    const inProgress = screen.getByTestId("column-in_progress");
    expect(within(inProgress).getByText("Add logout")).toBeInTheDocument();
    expect(within(inProgress).getByText("Fix crash")).toBeInTheDocument();
    // Column count badge.
    expect(within(inProgress).getByTestId("column-count")).toHaveTextContent("2");
  });

  it("moves a card to the next status via the forward control", () => {
    const onStatusChange = vi.fn();
    render(<KanbanBoard tasks={tasks} onStatusChange={onStatusChange} />);

    const card = screen.getByTestId("card-t1");
    fireEvent.click(within(card).getByRole("button", { name: /move forward/i }));
    expect(onStatusChange).toHaveBeenCalledWith("t1", "ready");
  });

  it("moves a card to the previous status via the back control", () => {
    const onStatusChange = vi.fn();
    render(<KanbanBoard tasks={tasks} onStatusChange={onStatusChange} />);

    const card = screen.getByTestId("card-t2");
    fireEvent.click(within(card).getByRole("button", { name: /move back/i }));
    expect(onStatusChange).toHaveBeenCalledWith("t2", "ready_for_agent");
  });
});
