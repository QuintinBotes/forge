import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TaskDTO } from "@/lib/api/types";
import { DepthKanban } from "./depth-kanban";

const tasks: TaskDTO[] = [
  { id: "t1", key: "FORGE-1", title: "Build login", status: "backlog" },
  { id: "t2", key: "FORGE-2", title: "Add signup", status: "backlog" },
  { id: "t3", key: "FORGE-3", title: "Fix crash", status: "in_progress" },
];

function renderKanban(
  props: Partial<React.ComponentProps<typeof DepthKanban>> = {},
) {
  const onToggleSelect = vi.fn();
  const onStatusChange = vi.fn();
  const onBulkStatus = vi.fn();
  render(
    <DepthKanban
      tasks={tasks}
      selection={props.selection ?? new Set()}
      onToggleSelect={onToggleSelect}
      onStatusChange={onStatusChange}
      onBulkStatus={onBulkStatus}
      {...props}
    />,
  );
  return { onToggleSelect, onStatusChange, onBulkStatus };
}

describe("DepthKanban", () => {
  it("renders a column per status with cards and counts", () => {
    renderKanban();
    const backlog = screen.getByTestId("column-backlog");
    expect(within(backlog).getByText("Build login")).toBeInTheDocument();
    expect(within(backlog).getByTestId("column-count")).toHaveTextContent("2");
    expect(
      within(screen.getByTestId("column-in_progress")).getByText("Fix crash"),
    ).toBeInTheDocument();
  });

  it("toggles selection from a card checkbox", () => {
    const { onToggleSelect } = renderKanban();
    fireEvent.click(screen.getByLabelText("Select Build login"));
    expect(onToggleSelect).toHaveBeenCalledWith("t1");
  });

  it("advances a card via the legal forward move button", () => {
    const { onStatusChange } = renderKanban();
    fireEvent.click(
      within(screen.getByTestId("card-t1")).getByRole("button", {
        name: /move forward/i,
      }),
    );
    expect(onStatusChange).toHaveBeenCalledWith("t1", "ready");
  });

  it("drops a card onto a legal column and moves it there", () => {
    const { onStatusChange } = renderKanban();
    fireEvent.dragStart(screen.getByTestId("card-t1"));
    fireEvent.drop(screen.getByTestId("column-ready"));
    expect(onStatusChange).toHaveBeenCalledWith("t1", "ready");
  });

  it("marks illegal columns and refuses the drop (column status rules)", () => {
    const { onStatusChange } = renderKanban();
    fireEvent.dragStart(screen.getByTestId("card-t1"));
    // backlog → done is not a legal transition.
    expect(screen.getByTestId("column-done")).toHaveAttribute(
      "data-legal-drop",
      "no",
    );
    expect(screen.getByTestId("column-ready")).toHaveAttribute(
      "data-legal-drop",
      "yes",
    );
    fireEvent.drop(screen.getByTestId("column-done"));
    expect(onStatusChange).not.toHaveBeenCalled();
  });

  it("moves the whole selection when a selected card is dragged", () => {
    const { onBulkStatus, onStatusChange } = renderKanban({
      selection: new Set(["t1", "t2"]),
    });
    fireEvent.dragStart(screen.getByTestId("card-t1"));
    fireEvent.drop(screen.getByTestId("column-ready"));
    expect(onBulkStatus).toHaveBeenCalledWith(["t1", "t2"], "ready");
    expect(onStatusChange).not.toHaveBeenCalled();
  });
});
