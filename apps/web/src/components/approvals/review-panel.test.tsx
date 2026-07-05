import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ApprovalContext, ApprovalSummary } from "@/lib/api/types";

import { ReviewPanel } from "./review-panel";

const summary: ApprovalSummary = {
  id: "a1",
  gate_type: "pr",
  status: "pending",
  title: "Merge auth refactor",
  risk_level: "warning",
  requested_actor: "agent:1111",
  requested_at: "2026-07-05T11:00:00Z",
};

const fullContext: ApprovalContext = {
  approval_id: "a1",
  gate_type: "pr",
  goal: "Ship the passwordless auth refactor",
  requirements: [{ text: "Support WebAuthn", ref: "SPEC-12" }],
  diff: {
    files_changed: 2,
    additions: 120,
    deletions: 30,
    files: [
      { path: "src/auth/login.ts", additions: 100, deletions: 20, status: "modified" },
      { path: "src/auth/webauthn.ts", additions: 20, deletions: 10, status: "added" },
    ],
  },
  verification: {
    lint: { status: "passed" },
    typecheck: false,
    tests: { passed: 42, total: 42 },
    coverage: { coverage: 0.87 },
  },
  traceability: [
    { requirement: "Support WebAuthn", spec_ref: "SPEC-12", covered: true },
    { requirement: "Rate-limit attempts", covered: false },
  ],
  knowledge_refs: [
    { title: "Auth ADR", path: "docs/adr/auth.md", score: 0.91 },
  ],
  confidence: { score: 0.82, rationale: "Strong test coverage and clear traceability." },
  risk_flags: [
    {
      severity: "critical",
      category: "security",
      message: "Touches the credential store",
      source: "policy",
    },
  ],
  run_trace_ref: { workflow_run_id: "wf-1", agent_run_id: "ag-1" },
  available_actions: ["approve", "reject", "request_changes"],
};

function renderPanel(context: ApprovalContext | undefined, extra = {}) {
  return render(
    <ReviewPanel
      summary={summary}
      context={context}
      isLoading={false}
      isError={false}
      {...extra}
    />,
  );
}

describe("ReviewPanel — nine must-show items", () => {
  it("renders the goal, requirements and diff files", () => {
    renderPanel(fullContext);
    expect(screen.getByTestId("review-panel")).toBeInTheDocument();
    expect(
      screen.getByText("Ship the passwordless auth refactor"),
    ).toBeInTheDocument();
    // "Support WebAuthn" also appears in traceability — scope to the goal section.
    expect(
      within(screen.getByTestId("review-section-1")).getByText("Support WebAuthn"),
    ).toBeInTheDocument();

    const diff = screen.getByTestId("diff-files");
    expect(within(diff).getByText("src/auth/login.ts")).toBeInTheDocument();
    expect(within(diff).getByText("+100")).toBeInTheDocument();
  });

  it("renders verification checks with pass/fail detail", () => {
    renderPanel(fullContext);
    const grid = screen.getByTestId("verification-grid");
    expect(within(grid).getByText("Lint")).toBeInTheDocument();
    expect(within(grid).getByText("Coverage")).toBeInTheDocument();
    expect(within(grid).getByText("87%")).toBeInTheDocument();
    expect(within(grid).getByText("42/42")).toBeInTheDocument();
  });

  it("renders traceability, knowledge, confidence, risks and run trace", () => {
    renderPanel(fullContext);
    expect(screen.getByText("Covered")).toBeInTheDocument();
    expect(screen.getByText("Missing")).toBeInTheDocument();
    expect(screen.getByText("Auth ADR")).toBeInTheDocument();

    const meter = screen.getByRole("meter", { name: /confidence/i });
    expect(meter).toHaveAttribute("aria-valuenow", "82");

    const risks = screen.getByTestId("risk-flags");
    expect(within(risks).getByText("Touches the credential store")).toBeInTheDocument();

    const trace = screen.getByTestId("run-trace");
    expect(within(trace).getByText("wf-1")).toBeInTheDocument();
  });

  it("hides sections that do not apply but always shows the risks section", () => {
    renderPanel({
      approval_id: "a1",
      gate_type: "policy_override",
      goal: "Allow a one-off prod migration",
      risk_flags: [],
      available_actions: ["approve", "reject", "request_changes", "escalate"],
    });
    expect(screen.queryByTestId("review-section-2")).not.toBeInTheDocument(); // no diff
    expect(screen.getByTestId("review-section-7")).toBeInTheDocument(); // risks
    expect(screen.getByText("No risks flagged.")).toBeInTheDocument();
  });

  it("shows a skeleton while loading", () => {
    render(
      <ReviewPanel
        summary={summary}
        context={undefined}
        isLoading
        isError={false}
      />,
    );
    expect(screen.getByTestId("review-skeleton")).toBeInTheDocument();
  });

  it("shows an error state when the context fails to load", () => {
    render(
      <ReviewPanel
        summary={summary}
        context={undefined}
        isLoading={false}
        isError
      />,
    );
    expect(screen.getByTestId("review-error")).toBeInTheDocument();
  });
});
