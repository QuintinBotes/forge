import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApprovalAction } from "@/lib/api/types";

import { DecisionBar } from "./decision-bar";

const ACTIONS: ApprovalAction[] = ["approve", "request_changes", "reject", "escalate"];

function setup(overrides: Partial<React.ComponentProps<typeof DecisionBar>> = {}) {
  const props = {
    actions: ACTIONS,
    activeNote: null,
    note: "",
    onNoteChange: vi.fn(),
    pending: false,
    errorMessage: null,
    onTrigger: vi.fn(),
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  } as React.ComponentProps<typeof DecisionBar>;
  render(<DecisionBar {...props} />);
  return props;
}

describe("DecisionBar", () => {
  it("renders every available action, with Approve as the single ember primary", () => {
    setup();
    const approve = screen.getByTestId("decision-approve");
    expect(approve).toBeInTheDocument();
    // Ember is precious: only Approve carries the primary surface.
    expect(approve.className).toContain("bg-primary");
    expect(screen.getByTestId("decision-reject").className).not.toContain(
      "bg-primary",
    );
    expect(screen.getByTestId("decision-escalate")).toBeInTheDocument();
  });

  it("triggers an action on click", () => {
    const props = setup();
    fireEvent.click(screen.getByTestId("decision-approve"));
    expect(props.onTrigger).toHaveBeenCalledWith("approve");
  });

  it("shows the reason composer and confirms/cancels", () => {
    const props = setup({ activeNote: "reject", note: "not safe" });
    const composer = screen.getByTestId("reason-composer");
    expect(composer).toBeInTheDocument();
    // Action buttons are replaced by the composer.
    expect(screen.queryByTestId("decision-approve")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("confirm-decision"));
    expect(props.onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("confirms the composed reason on Cmd/Ctrl+Enter and cancels on Escape", () => {
    const props = setup({ activeNote: "request_changes", note: "tweak it" });
    const textarea = screen.getByLabelText(/what needs to change/i);
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("surfaces a decision error", () => {
    setup({ errorMessage: "This gate was already resolved." });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This gate was already resolved.",
    );
  });
});
