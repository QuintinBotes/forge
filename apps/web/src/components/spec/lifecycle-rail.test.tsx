import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LifecycleRail } from "./lifecycle-rail";

describe("LifecycleRail", () => {
  it("renders all six SDD stages", () => {
    render(<LifecycleRail status="approved" />);
    for (const label of [
      "Draft",
      "Clarifying",
      "Approved",
      "Implementing",
      "Validated",
      "Closed",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("marks the current stage, past stages done, and future stages upcoming", () => {
    render(<LifecycleRail status="approved" />);
    expect(screen.getByTestId("stage-approved")).toHaveAttribute("data-state", "current");
    expect(screen.getByTestId("stage-approved")).toHaveAttribute("aria-current", "step");
    expect(screen.getByTestId("stage-draft")).toHaveAttribute("data-state", "done");
    expect(screen.getByTestId("stage-validated")).toHaveAttribute("data-state", "upcoming");
  });

  it("defaults an unknown status to the draft stage", () => {
    render(<LifecycleRail status={undefined} />);
    expect(screen.getByTestId("stage-draft")).toHaveAttribute("data-state", "current");
  });
});
