import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BulkActionBar } from "./bulk-action-bar";

function renderBar(overrides: Partial<React.ComponentProps<typeof BulkActionBar>> = {}) {
  const onSetStatus = vi.fn();
  const onAssignToMe = vi.fn();
  const onClear = vi.fn();
  render(
    <BulkActionBar
      count={3}
      onSetStatus={onSetStatus}
      onAssignToMe={onAssignToMe}
      canAssign
      onClear={onClear}
      {...overrides}
    />,
  );
  return { onSetStatus, onAssignToMe, onClear };
}

describe("BulkActionBar", () => {
  it("renders nothing when nothing is selected", () => {
    const { container } = render(
      <BulkActionBar
        count={0}
        onSetStatus={vi.fn()}
        onAssignToMe={vi.fn()}
        canAssign
        onClear={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the selection count", () => {
    renderBar();
    expect(screen.getByRole("region", { name: /bulk actions/i })).toHaveTextContent(
      "3 selected",
    );
  });

  it("applies a bulk status change", () => {
    const { onSetStatus } = renderBar();
    fireEvent.change(screen.getByLabelText(/set status for selected/i), {
      target: { value: "done" },
    });
    expect(onSetStatus).toHaveBeenCalledWith("done");
  });

  it("assigns to me, and disables it when the viewer is unknown", () => {
    const { onAssignToMe } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: /assign to me/i }));
    expect(onAssignToMe).toHaveBeenCalled();

    render(
      <BulkActionBar
        count={1}
        onSetStatus={vi.fn()}
        onAssignToMe={vi.fn()}
        canAssign={false}
        onClear={vi.fn()}
      />,
    );
    expect(screen.getAllByRole("button", { name: /assign to me/i }).at(-1)).toBeDisabled();
  });

  it("clears the selection", () => {
    const { onClear } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect(onClear).toHaveBeenCalled();
  });
});
