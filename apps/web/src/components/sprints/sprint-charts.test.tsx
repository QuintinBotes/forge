import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  BurndownPoint,
  CFDPoint,
  GoalAlignment,
  MemberAllocation,
  VelocitySprintBar,
} from "@/lib/api/types";

import {
  BurndownChart,
  CapacityBars,
  CFDChart,
  GoalAlignmentMeter,
  VelocityChart,
} from "./sprint-charts";

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

describe("CapacityBars", () => {
  const MEMBERS: MemberAllocation[] = [
    {
      member_id: "alice",
      capacity_points: 5,
      assigned_points: 8,
      utilization: 1.6,
      status: "over",
    },
    {
      member_id: "bob",
      capacity_points: 8,
      assigned_points: 2,
      utilization: 0.25,
      status: "under",
    },
  ];

  it("renders one row per member with its allocation status", () => {
    render(<CapacityBars members={MEMBERS} testId="cb" />);
    const list = screen.getByTestId("cb");
    expect(within(list).getByText("alice")).toBeInTheDocument();
    expect(within(list).getByText("Over-allocated")).toBeInTheDocument();
    expect(within(list).getByText("Under-allocated")).toBeInTheDocument();
    expect(within(list).getByText("160%")).toBeInTheDocument();
  });

  it("renders nothing for an empty member list", () => {
    const { container } = render(<CapacityBars members={[]} testId="cb" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("never hardcodes a hex colour (design tokens only)", () => {
    const { container } = render(<CapacityBars members={MEMBERS} testId="cb" />);
    expect(container.innerHTML).not.toMatch(/#[0-9a-f]{3,}/i);
  });
});

describe("GoalAlignmentMeter", () => {
  const ALIGNMENT: GoalAlignment = {
    sprint_id: "s1",
    goal_tokens: ["ship", "forge"],
    total_count: 4,
    aligned_count: 3,
    alignment_ratio: 0.75,
    unaligned_task_ids: ["t-9"],
  };

  it("renders the alignment ratio and the unaligned count", () => {
    render(<GoalAlignmentMeter alignment={ALIGNMENT} testId="ga" />);
    const meter = screen.getByTestId("ga");
    expect(meter).toHaveTextContent("75%");
    expect(meter).toHaveTextContent("3 of 4 tasks aligned");
    expect(meter).toHaveTextContent("1 task shares no keyword with the goal.");
  });

  it("omits the unaligned note when every task is aligned", () => {
    render(
      <GoalAlignmentMeter
        alignment={{ ...ALIGNMENT, aligned_count: 4, alignment_ratio: 1, unaligned_task_ids: [] }}
        testId="ga"
      />,
    );
    expect(screen.queryByText(/shares no keyword/)).not.toBeInTheDocument();
  });

  it("never hardcodes a hex colour (design tokens only)", () => {
    const { container } = render(<GoalAlignmentMeter alignment={ALIGNMENT} testId="ga" />);
    expect(container.innerHTML).not.toMatch(/#[0-9a-f]{3,}/i);
  });
});

describe("CFDChart", () => {
  const POINTS: CFDPoint[] = [
    { snapshot_date: "2026-06-01", status_counts: { backlog: 4, done: 0 } },
    { snapshot_date: "2026-06-02", status_counts: { backlog: 3, done: 1 } },
    { snapshot_date: "2026-06-03", status_counts: { backlog: 2, done: 2 } },
  ];

  it("renders one legend entry per status and the SVG figure", () => {
    render(<CFDChart points={POINTS} testId="cfd" />);
    const legend = screen.getByLabelText("Series legend");
    expect(legend).toHaveTextContent("backlog");
    expect(legend).toHaveTextContent("done");
    expect(
      screen.getByRole("img", { name: /cumulative flow diagram/i }),
    ).toBeInTheDocument();
  });

  it("exposes a daily table with one column per status", () => {
    render(<CFDChart points={POINTS} testId="cfd" />);
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: /cumulative flow by day/i });
    expect(within(table).getByText("backlog")).toBeInTheDocument();
    expect(within(table).getByText("done")).toBeInTheDocument();
    expect(table).toHaveTextContent("Jun 3");
  });

  it("renders nothing for an empty series", () => {
    const { container } = render(<CFDChart points={[]} testId="cfd" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("never hardcodes a hex colour (design tokens only)", () => {
    const { container } = render(<CFDChart points={POINTS} testId="cfd" />);
    expect(container.innerHTML).not.toMatch(/#[0-9a-f]{3,}/i);
  });
});
