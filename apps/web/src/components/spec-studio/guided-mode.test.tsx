import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { SpecManifest } from "@/lib/api/types";

import { GuidedMode } from "./guided-mode";

function Harness({ initial }: { initial: SpecManifest }) {
  const [value, setValue] = useState(initial);
  return <GuidedMode value={value} onChange={setValue} onSave={vi.fn()} dirty />;
}

const baseManifest: SpecManifest = {
  id: "s1",
  name: "Passwordless auth",
  requirements: [{ id: "R1", text: "Sign in without a password" }],
};

describe("GuidedMode", () => {
  it("renders the Goal, Requirements, Acceptance Criteria and Constraints blocks", () => {
    render(<Harness initial={baseManifest} />);
    expect(screen.getByTestId("guided-name")).toHaveValue("Passwordless auth");
    expect(screen.getByTestId("guided-requirements")).toBeInTheDocument();
    expect(screen.getByTestId("guided-acceptance-criteria")).toBeInTheDocument();
    expect(screen.getByTestId("guided-constraints")).toBeInTheDocument();
  });

  it("auto-numbers a newly added requirement without a text input for its id", () => {
    render(<Harness initial={{ id: "s1", name: "x" }} />);
    fireEvent.click(screen.getByTestId("guided-add-requirement"));
    expect(screen.getByTestId("requirement-id-0")).toHaveTextContent("R1");
    fireEvent.click(screen.getByTestId("guided-add-requirement"));
    expect(screen.getByTestId("requirement-id-1")).toHaveTextContent("R2");
  });

  it("adds an acceptance criterion with Given/When/Then fields and auto-numbered id", () => {
    render(<Harness initial={baseManifest} />);
    fireEvent.click(screen.getByTestId("guided-add-acceptance-criterion"));
    expect(screen.getByTestId("acceptance-criterion-id-0")).toHaveTextContent("AC1");
    fireEvent.change(screen.getByLabelText("AC1 given"), { target: { value: "a user" } });
    fireEvent.change(screen.getByLabelText("AC1 when"), { target: { value: "they sign in" } });
    fireEvent.change(screen.getByLabelText("AC1 then"), { target: { value: "they land on the board" } });
    expect(screen.getByLabelText("AC1 given")).toHaveValue("a user");
    expect(screen.getByLabelText("AC1 when")).toHaveValue("they sign in");
    expect(screen.getByLabelText("AC1 then")).toHaveValue("they land on the board");
  });

  it("links an acceptance criterion to a requirement via the dropdown, not free text", () => {
    render(<Harness initial={baseManifest} />);
    fireEvent.click(screen.getByTestId("guided-add-acceptance-criterion"));

    // No free-text "(R#)" entry point exists — only the link dropdown.
    expect(screen.queryByLabelText(/req_refs/i)).not.toBeInTheDocument();

    const select = screen.getByTestId("ac-link-requirement-0");
    fireEvent.change(select, { target: { value: "R1" } });

    expect(screen.getByTestId("ac-linked-req-0-R1")).toHaveTextContent("R1");
    // Once linked, R1 is no longer offered again in the dropdown.
    expect(screen.queryByRole("option", { name: /R1/ })).not.toBeInTheDocument();
  });

  it("unlinks a requirement from an acceptance criterion", () => {
    render(
      <Harness
        initial={{
          ...baseManifest,
          acceptance_criteria: [{ id: "AC1", text: "Then it works", req_refs: ["R1"] }],
        }}
      />,
    );
    expect(screen.getByTestId("ac-linked-req-0-R1")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Unlink R1 from AC1"));
    expect(screen.queryByTestId("ac-linked-req-0-R1")).not.toBeInTheDocument();
  });

  it("keeps Advanced collapsed by default and reveals it on toggle", () => {
    render(<Harness initial={baseManifest} />);
    expect(screen.queryByTestId("guided-advanced-panel")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("guided-advanced-toggle"));
    expect(screen.getByTestId("guided-advanced-panel")).toBeInTheDocument();
    expect(screen.getByTestId("guided-execution-mode")).toBeInTheDocument();
    expect(screen.getByTestId("guided-constitution-refs")).toBeInTheDocument();
    expect(screen.getByTestId("guided-repos")).toBeInTheDocument();
    expect(screen.getByTestId("guided-decisions")).toBeInTheDocument();
  });

  it("surfaces validation gaps as non-blocking nudges", () => {
    render(<Harness initial={baseManifest} />);
    expect(screen.getByTestId("guided-nudge-uncovered-R1")).toBeInTheDocument();
    // Nudges never disable Save; that's governed by `dirty`, not nudge count.
    expect(screen.getByTestId("guided-save")).toBeEnabled();
  });

  it("clears the coverage nudge once every requirement is linked", () => {
    render(
      <Harness
        initial={{
          ...baseManifest,
          acceptance_criteria: [{ id: "AC1", text: "Then it works", req_refs: ["R1"] }],
        }}
      />,
    );
    expect(screen.queryByTestId("guided-nudge-uncovered-R1")).not.toBeInTheDocument();
  });

  it("shows a Ready-to-create checklist and coverage meter", () => {
    render(<Harness initial={baseManifest} />);
    expect(screen.getByTestId("guided-coverage-meter")).toHaveTextContent("0/1 requirements covered (0%)");
    expect(screen.getByTestId("checklist-item-goal")).toBeInTheDocument();
    expect(screen.getByTestId("checklist-item-coverage")).toBeInTheDocument();
  });

  it("updates the coverage meter as requirements get linked", () => {
    render(
      <Harness
        initial={{
          ...baseManifest,
          acceptance_criteria: [{ id: "AC1", text: "Then it works", req_refs: ["R1"] }],
        }}
      />,
    );
    expect(screen.getByTestId("guided-coverage-meter")).toHaveTextContent(
      "1/1 requirements covered (100%)",
    );
  });
});
