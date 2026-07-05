import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BurndownPoint, VelocitySprintBar } from "@/lib/api/types";

import { BurndownChart, VelocityChart } from "./sprint-charts";

const BARS: VelocitySprintBar[] = [
  {
    sprint_id: "s5",
    name: "Sprint 5",
    end_date: "2026-05-03",
    committed_points: 30,
    completed_points: 28,
    predictability: 0.93,
  },
  {
    sprint_id: "s6",
    name: "Sprint 6",
    end_date: "2026-05-17",
    committed_points: 32,
    completed_points: 24,
    predictability: 0.75,
  },
];

const POINTS: BurndownPoint[] = [
  {
    snapshot_date: "2026-06-01",
    scope_points: 34,
    remaining_points: 34,
    completed_points: 0,
    ideal_points: 34,
    completed_task_count: 0,
    remaining_task_count: 8,
  },
  {
    snapshot_date: "2026-06-02",
    scope_points: 34,
    remaining_points: 27,
    completed_points: 7,
    ideal_points: 28,
    completed_task_count: 1,
    remaining_task_count: 7,
  },
  {
    snapshot_date: "2026-06-03",
    scope_points: 36,
    remaining_points: 18,
    completed_points: 16,
    ideal_points: 22,
    completed_task_count: 3,
    remaining_task_count: 5,
  },
];

describe("VelocityChart", () => {
  it("renders a two-series legend and the SVG figure", () => {
    render(<VelocityChart bars={BARS} averageVelocity={26} testId="vc" />);
    const legend = screen.getByLabelText("Series legend");
    expect(legend).toHaveTextContent("Committed");
    expect(legend).toHaveTextContent("Completed");
    expect(
      screen.getByRole("img", { name: /committed versus completed/i }),
    ).toBeInTheDocument();
  });

  it("reveals the data table for accessibility", () => {
    render(<VelocityChart bars={BARS} testId="vc" />);
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: /velocity by sprint/i });
    expect(table).toHaveTextContent("Sprint 5");
    expect(table).toHaveTextContent("Sprint 6");
    // Predictability rendered as a percent in the table.
    expect(table).toHaveTextContent("93%");
  });

  it("shows a per-sprint tooltip on hover", () => {
    render(<VelocityChart bars={BARS} testId="vc" />);
    const bands = screen
      .getByRole("img", { name: /committed versus completed/i })
      .querySelectorAll("rect");
    fireEvent.mouseEnter(bands[bands.length - 1]);
    const tip = screen.getByTestId("velocity-tooltip");
    expect(tip).toHaveTextContent("Sprint 6");
    expect(tip).toHaveTextContent("Committed");
    expect(tip).toHaveTextContent("32 pts");
  });
});

describe("BurndownChart", () => {
  it("renders remaining and ideal series in the legend", () => {
    render(<BurndownChart points={POINTS} testId="bc" />);
    const legend = screen.getByLabelText("Series legend");
    expect(legend).toHaveTextContent("Remaining");
    expect(legend).toHaveTextContent("Ideal");
    expect(
      screen.getByRole("img", { name: /remaining story points/i }),
    ).toBeInTheDocument();
  });

  it("exposes the daily table with scope alongside remaining and ideal", () => {
    render(<BurndownChart points={POINTS} testId="bc" />);
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: /burndown by day/i });
    expect(within(table).getByText("Scope")).toBeInTheDocument();
    // Day 3 remaining = 18.
    expect(table).toHaveTextContent("18");
    expect(table).toHaveTextContent("Jun 3");
  });

  it("shows a crosshair tooltip on hover", () => {
    render(<BurndownChart points={POINTS} testId="bc" />);
    const bands = screen
      .getByRole("img", { name: /remaining story points/i })
      .querySelectorAll("rect");
    fireEvent.mouseEnter(bands[0]);
    const tip = screen.getByTestId("burndown-tooltip");
    expect(tip).toHaveTextContent("Jun 1");
    expect(tip).toHaveTextContent("Remaining");
  });
});
