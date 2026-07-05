import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RemediationPlanView } from "@/lib/api/types";

import { RemediationPanel } from "./remediation-panel";

function plan(overrides: Partial<RemediationPlanView> = {}): RemediationPlanView {
  return {
    id: "p1",
    incident_id: "i1",
    attempt: 1,
    max_blast_radius: "medium",
    status: "proposed",
    offending_step_ids: [],
    steps: [
      {
        id: "s1",
        order: 1,
        title: "Roll back checkout deploy",
        action: "deploy.rollback",
        blast_radius: "medium",
        rationale: "Latest deploy correlates with the latency spike.",
        status: "proposed",
        blocked: false,
      },
    ],
    ...overrides,
  };
}

describe("RemediationPanel", () => {
  it("renders the plan, steps and a within-policy banner", () => {
    render(<RemediationPanel plan={plan()} isLoading={false} isError={false} />);
    expect(screen.getByTestId("remediation-plan")).toBeInTheDocument();
    expect(screen.getByText(/roll back checkout deploy/i)).toBeInTheDocument();
    expect(screen.getByText(/deploy\.rollback/)).toBeInTheDocument();
    expect(screen.getByText(/within the incident's blast-radius policy/i)).toBeInTheDocument();
  });

  it("flags a plan with offending steps as blocked", () => {
    const blocked = plan({
      offending_step_ids: ["s1"],
      steps: [{ ...plan().steps[0], blocked: true, blast_radius: "high" }],
    });
    render(<RemediationPanel plan={blocked} isLoading={false} isError={false} />);
    expect(screen.getByText(/exceed the incident's blast-radius policy/i)).toBeInTheDocument();
    expect(screen.getByTestId("remediation-step")).toHaveAttribute("data-blocked", "true");
    expect(screen.getByText(/outside blast-radius policy/i)).toBeInTheDocument();
  });

  it("shows the empty state when no plan is proposed", () => {
    render(<RemediationPanel plan={null} isLoading={false} isError={false} />);
    expect(screen.getByTestId("remediation-empty")).toBeInTheDocument();
    expect(screen.getByText(/no remediation proposed yet/i)).toBeInTheDocument();
  });

  it("renders a loading skeleton", () => {
    render(<RemediationPanel plan={undefined} isLoading isError={false} />);
    expect(screen.getByTestId("remediation-skeleton")).toBeInTheDocument();
  });

  it("renders an error state", () => {
    render(<RemediationPanel plan={undefined} isLoading={false} isError />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn't load the remediation plan/i);
  });
});
