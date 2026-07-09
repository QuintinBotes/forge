import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EpicDTO, MilestoneDTO, SprintDTO, TaskDTO } from "@/lib/api/types";
import { DepthRoadmap } from "./depth-roadmap";

const epics: EpicDTO[] = [{ id: "e1", title: "Auth" }];
const sprints: SprintDTO[] = [
  { id: "s1", name: "Sprint 1", starts_at: "2026-01-01", ends_at: "2026-01-14" },
];
const milestones: MilestoneDTO[] = [
  { id: "m1", name: "Beta", due_at: "2026-01-10" },
];
const tasks: TaskDTO[] = [
  { id: "t1", title: "Login", status: "in_progress", epic_id: "e1", sprint_id: "s1" },
];

describe("DepthRoadmap", () => {
  it("renders an empty state when nothing is scheduled", () => {
    render(<DepthRoadmap tasks={[]} epics={epics} sprints={sprints} milestones={milestones} />);
    expect(screen.getByTestId("roadmap-empty")).toBeInTheDocument();
  });

  it("lays out epic lanes, sprint columns, milestones and cells", () => {
    render(
      <DepthRoadmap tasks={tasks} epics={epics} sprints={sprints} milestones={milestones} />,
    );
    expect(screen.getByTestId("roadmap")).toBeInTheDocument();
    expect(screen.getByTestId("lane-e1")).toHaveTextContent("Auth");
    expect(screen.getByText("Sprint 1")).toBeInTheDocument();
    expect(screen.getByTestId("milestone-m1")).toHaveTextContent("Beta");
    expect(within(screen.getByTestId("cell-e1-s1")).getByText("Login")).toBeInTheDocument();
  });

  it("gives every real epic lane a 'Create spec' action pointing at /specs/new", () => {
    render(
      <DepthRoadmap tasks={tasks} epics={epics} sprints={sprints} milestones={milestones} />,
    );
    const link = screen.getByTestId("lane-create-spec-e1");
    expect(link).toHaveAttribute("href", "/specs/new?epicId=e1");
  });

  it("does not offer 'Create spec' on the synthetic 'No epic' lane", () => {
    const unepiced: TaskDTO[] = [{ id: "t2", title: "Stray", status: "backlog" }];
    render(
      <DepthRoadmap tasks={unepiced} epics={epics} sprints={sprints} milestones={milestones} />,
    );
    expect(screen.queryByTestId(/lane-create-spec-__no_epic__/)).not.toBeInTheDocument();
  });
});
