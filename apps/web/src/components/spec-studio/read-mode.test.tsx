import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SpecManifest } from "@/lib/api/types";

import { ReadMode } from "./read-mode";

const baseSpec: SpecManifest = {
  id: "SPEC-1",
  name: "Passwordless auth",
  status: "draft",
  requirements: [{ id: "R1", text: "Sign in without a password" }],
  acceptance_criteria: [
    { id: "AC1", text: "Given a valid magic link, when opened, then the user is signed in.", req_refs: ["R1"] },
  ],
  constraints: ["Must work offline"],
  open_questions: [{ id: "Q1", text: "What about shared devices?" }],
  decisions: [{ id: "ADR-1", title: "Use magic links", decision: "Adopt email magic links." }],
};

function setup(overrides: Partial<React.ComponentProps<typeof ReadMode>> = {}) {
  const props = {
    spec: baseSpec,
    onApprove: vi.fn(),
    ...overrides,
  } as React.ComponentProps<typeof ReadMode>;
  render(<ReadMode {...props} />);
  return props;
}

describe("ReadMode", () => {
  it("renders clean rendered prose for every populated spec.md section", () => {
    setup();
    const prose = screen.getByTestId("read-prose");
    expect(prose).toHaveTextContent("Passwordless auth");
    expect(prose).toHaveTextContent("Sign in without a password");
    expect(prose).toHaveTextContent("Given a valid magic link");
    expect(prose).toHaveTextContent("AC1 (R1)");
    expect(prose).toHaveTextContent("Must work offline");
    expect(prose).toHaveTextContent("What about shared devices?");
    expect(prose).toHaveTextContent("Use magic links");
  });

  it("keeps the full manifest facts panel one disclosure away", () => {
    setup();
    expect(screen.getByTestId("manifest-panel")).toBeInTheDocument();
  });

  it("shows the spec's current lifecycle status", () => {
    setup({ spec: { ...baseSpec, status: "clarifying" } });
    expect(screen.getByTestId("read-status")).toHaveTextContent("Clarifying");
  });

  it("clicking Approve calls onApprove directly (no note needed)", () => {
    const onApprove = vi.fn();
    setup({ onApprove });
    fireEvent.click(screen.getByTestId("decision-approve"));
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it("pressing 'a' approves via keyboard shortcut", () => {
    const onApprove = vi.fn();
    setup({ onApprove });
    fireEvent.keyDown(screen.getByTestId("read-mode"), { key: "a" });
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it("pressing 'x' opens the reject note composer, and confirming records + calls onReject", () => {
    const onReject = vi.fn();
    setup({ onReject });
    fireEvent.keyDown(screen.getByTestId("read-mode"), { key: "x" });
    const composer = screen.getByTestId("reason-composer");
    fireEvent.change(composer.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Missing offline handling" },
    });
    fireEvent.click(screen.getByTestId("confirm-decision"));
    expect(onReject).toHaveBeenCalledWith("Missing offline handling");
    expect(screen.getByTestId("review-recorded")).toHaveTextContent("Rejected");
    expect(screen.getByTestId("review-recorded")).toHaveTextContent("Missing offline handling");
  });

  it("pressing 'r' opens the request-changes note composer, and confirming records + calls onRequestChanges", () => {
    const onRequestChanges = vi.fn();
    setup({ onRequestChanges });
    fireEvent.keyDown(screen.getByTestId("read-mode"), { key: "r" });
    const composer = screen.getByTestId("reason-composer");
    fireEvent.change(composer.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Please add a rate limit" },
    });
    fireEvent.click(screen.getByTestId("confirm-decision"));
    expect(onRequestChanges).toHaveBeenCalledWith("Please add a rate limit");
    expect(screen.getByTestId("review-recorded")).toHaveTextContent("Changes requested");
  });

  it("Escape cancels the note composer without recording a decision", () => {
    const onReject = vi.fn();
    setup({ onReject });
    fireEvent.keyDown(screen.getByTestId("read-mode"), { key: "x" });
    fireEvent.keyDown(screen.getByTestId("reason-composer").querySelector("textarea") as HTMLTextAreaElement, {
      key: "Escape",
    });
    expect(screen.queryByTestId("reason-composer")).not.toBeInTheDocument();
    expect(onReject).not.toHaveBeenCalled();
    expect(screen.queryByTestId("review-recorded")).not.toBeInTheDocument();
  });

  it("disables the decision bar once the spec is past the human gate", () => {
    setup({ spec: { ...baseSpec, status: "approved" } });
    expect(screen.getByTestId("review-gate-closed")).toBeInTheDocument();
    expect(screen.getByTestId("decision-approve")).toBeDisabled();
  });

  it("does not react to keyboard shortcuts once past the human gate", () => {
    const onApprove = vi.fn();
    setup({ spec: { ...baseSpec, status: "approved" }, onApprove });
    fireEvent.keyDown(screen.getByTestId("read-mode"), { key: "a" });
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("surfaces an approve error from the caller", () => {
    setup({ approveError: "Couldn't reach the spec engine" });
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't reach the spec engine");
  });

  it("shows a saving state on Approve while the mutation is pending", () => {
    setup({ approving: true });
    expect(screen.getByTestId("decision-approve")).toBeDisabled();
  });
});
