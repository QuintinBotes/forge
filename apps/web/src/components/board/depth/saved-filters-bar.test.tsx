import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { BoardView } from "@/lib/board/filters";
import { SavedFiltersBar } from "./saved-filters-bar";

const views: BoardView[] = [
  { id: "all", label: "All work" },
  { id: "blocked", label: "Blocked", statuses: ["blocked"] },
  { id: "saved-1", label: "My hotlist", priorities: ["urgent"] },
];

function renderBar(overrides: Partial<React.ComponentProps<typeof SavedFiltersBar>> = {}) {
  const onSelect = vi.fn();
  const onDelete = vi.fn();
  const onSaveCurrent = vi.fn();
  render(
    <SavedFiltersBar
      views={views}
      activeId="all"
      removableIds={new Set(["saved-1"])}
      onSelect={onSelect}
      onDelete={onDelete}
      onSaveCurrent={onSaveCurrent}
      canSave
      {...overrides}
    />,
  );
  return { onSelect, onDelete, onSaveCurrent };
}

describe("SavedFiltersBar", () => {
  it("marks the active view and selects on click", () => {
    const { onSelect } = renderBar();
    expect(screen.getByRole("button", { name: "All work" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(onSelect).toHaveBeenCalledWith(views[1]);
  });

  it("deletes a user-saved view", () => {
    const { onDelete } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: /delete my hotlist view/i }));
    expect(onDelete).toHaveBeenCalledWith("saved-1");
  });

  it("does not offer delete on preset views", () => {
    renderBar();
    expect(
      screen.queryByRole("button", { name: /delete all work view/i }),
    ).not.toBeInTheDocument();
  });

  it("saves the current filter as a named view", () => {
    const { onSaveCurrent } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: /save view/i }));
    fireEvent.change(screen.getByLabelText("View name"), {
      target: { value: "Sprint focus" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(onSaveCurrent).toHaveBeenCalledWith("Sprint focus");
  });

  it("disables saving when there is nothing to save", () => {
    renderBar({ canSave: false });
    expect(screen.getByRole("button", { name: /save view/i })).toBeDisabled();
  });
});
