import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApprovalSummary } from "@/lib/api/types";

import { ApprovalList } from "./approval-list";

const items: ApprovalSummary[] = [
  {
    id: "a1",
    gate_type: "pr",
    status: "pending",
    title: "Merge auth refactor",
    risk_level: "warning",
    requested_actor: "agent:1111",
    requested_at: "2026-07-05T11:00:00Z",
  },
  {
    id: "a2",
    gate_type: "deploy",
    status: "pending",
    title: "Promote to production",
    risk_level: "critical",
    requested_actor: "system",
    requested_at: "2026-07-05T10:00:00Z",
  },
];

describe("ApprovalList", () => {
  it("renders a row per pending gate with its gate label and risk", () => {
    render(<ApprovalList items={items} selectedId={null} onSelect={vi.fn()} />);
    expect(screen.getByText("Merge auth refactor")).toBeInTheDocument();
    expect(screen.getByText("Promote to production")).toBeInTheDocument();

    const row = screen.getByTestId("approval-row-a2");
    expect(within(row).getByText("Deploy")).toBeInTheDocument();
    expect(within(row).getByText("Critical")).toBeInTheDocument();
  });

  it("marks the selected row via aria-selected", () => {
    render(<ApprovalList items={items} selectedId="a2" onSelect={vi.fn()} />);
    expect(screen.getByTestId("approval-row-a2")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("approval-row-a1")).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("invokes onSelect when a row is clicked", () => {
    const onSelect = vi.fn();
    render(<ApprovalList items={items} selectedId={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("approval-row-a1"));
    expect(onSelect).toHaveBeenCalledWith(items[0]);
  });
});
