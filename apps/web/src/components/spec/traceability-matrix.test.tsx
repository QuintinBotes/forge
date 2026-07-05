import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RequirementTrace } from "@/lib/api/types";

import { TraceabilityMatrix } from "./traceability-matrix";

const traces: RequirementTrace[] = [
  {
    requirement_id: "REQ-1",
    text: "Users sign in without a password",
    acceptance_criteria_ids: ["AC-1"],
    task_refs: ["TASK-a1"],
    test_refs: ["test_login"],
    satisfied: true,
  },
  {
    requirement_id: "REQ-2",
    text: "Sessions expire after 24h",
    acceptance_criteria_ids: ["AC-2"],
    task_refs: ["TASK-b2"],
    test_refs: [],
    satisfied: true,
  },
  {
    requirement_id: "REQ-3",
    text: "Rate limit auth attempts",
    acceptance_criteria_ids: [],
    task_refs: [],
    test_refs: [],
    satisfied: false,
  },
];

describe("TraceabilityMatrix", () => {
  it("renders a row per requirement with its id, tasks and tests", () => {
    render(<TraceabilityMatrix traces={traces} />);
    expect(screen.getAllByTestId("trace-row")).toHaveLength(3);
    expect(screen.getByText("REQ-1")).toBeInTheDocument();
    expect(screen.getByText("TASK-a1")).toBeInTheDocument();
    expect(screen.getByText("test_login")).toBeInTheDocument();
  });

  it("seals a satisfied+tested requirement and flags the others", () => {
    render(<TraceabilityMatrix traces={traces} />);
    const rows = screen.getAllByTestId("trace-row");
    expect(within(rows[0]).getByText("Sealed")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Untested")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Open")).toBeInTheDocument();
    // The summary counts only the fully sealed requirement (1 of 3).
    expect(screen.getByTestId("trace-summary")).toHaveTextContent(/1 of 3 requirements sealed/i);
  });

  it("shows the empty state when there is no traceability", () => {
    render(<TraceabilityMatrix traces={[]} />);
    expect(screen.getByTestId("traceability-empty")).toBeInTheDocument();
    expect(screen.getByText(/no traceability yet/i)).toBeInTheDocument();
  });
});
