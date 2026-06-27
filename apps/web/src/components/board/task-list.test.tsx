import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TaskDTO } from "@/lib/api/types";
import { TaskList } from "./task-list";

const tasks: TaskDTO[] = [
  {
    id: "t1",
    key: "FORGE-1",
    title: "Build login",
    status: "backlog",
    priority: "high",
  },
  {
    id: "t2",
    key: "FORGE-2",
    title: "Add logout",
    status: "in_progress",
    priority: "medium",
  },
  {
    id: "t3",
    key: "FORGE-3",
    title: "Fix crash",
    status: "done",
    priority: "urgent",
  },
];

describe("TaskList", () => {
  it("renders a row per task with its title, key and status", () => {
    render(<TaskList tasks={tasks} />);
    expect(screen.getByText("Build login")).toBeInTheDocument();
    expect(screen.getByText("Add logout")).toBeInTheDocument();
    expect(screen.getByText("FORGE-1")).toBeInTheDocument();

    const row = screen.getByTestId("task-row-t2");
    expect(within(row).getByText("In progress")).toBeInTheDocument();
  });

  it("shows an empty state when there are no tasks", () => {
    render(<TaskList tasks={[]} />);
    expect(screen.getByText(/no tasks/i)).toBeInTheDocument();
  });

  it("supports keyboard navigation (j/k and arrows) to move the selection", () => {
    render(<TaskList tasks={tasks} />);
    const grid = screen.getByTestId("task-list");

    // No selection initially.
    expect(screen.getByTestId("task-row-t1")).toHaveAttribute(
      "aria-selected",
      "false",
    );

    fireEvent.keyDown(grid, { key: "ArrowDown" });
    expect(screen.getByTestId("task-row-t1")).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(grid, { key: "j" });
    expect(screen.getByTestId("task-row-t2")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("task-row-t1")).toHaveAttribute(
      "aria-selected",
      "false",
    );

    fireEvent.keyDown(grid, { key: "k" });
    expect(screen.getByTestId("task-row-t1")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("invokes onSelect when a row is activated", () => {
    const onSelect = vi.fn();
    render(<TaskList tasks={tasks} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("task-row-t3"));
    expect(onSelect).toHaveBeenCalledWith(tasks[2]);
  });
});
