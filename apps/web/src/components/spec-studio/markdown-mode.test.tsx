import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { MarkdownMode } from "./markdown-mode";

const VALID = `---
id: SPEC-1
status: draft
constitution_refs: []
repos: []
execution_mode: single_agent
skill_profile: null
plan_ref: null
tasks_ref: null
validation_ref: null
---

## Goal

Passwordless auth

## Requirements

- **R1**: Users can sign in without a password

## Acceptance Criteria

- **AC1** (R1): Given a valid magic link, when clicked, then the user is signed in

## Constraints

- Must work offline
`;

function Harness({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial);
  return <MarkdownMode value={value} onChange={setValue} onSave={vi.fn()} dirty={value !== initial} />;
}

describe("MarkdownMode", () => {
  it("shows a valid parse status and defaults to the Structure panel", () => {
    render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} dirty={false} />);
    expect(screen.getByTestId("markdown-status-valid")).toBeInTheDocument();
    expect(screen.getByTestId("markdown-panel-structure")).toBeInTheDocument();
    expect(screen.getByTestId("markdown-save")).toBeDisabled();
  });

  it("renders frontmatter and body verbatim in the raw textarea", () => {
    render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} />);
    const textarea = screen.getByTestId("markdown-textarea");
    expect(textarea).toHaveValue(VALID);
    expect((textarea as HTMLTextAreaElement).value).toContain("---\nid: SPEC-1");
  });

  it("renders a line-number gutter matching the text line count", () => {
    const { container } = render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} />);
    const gutterLines = container.querySelectorAll('[aria-hidden="true"] > div');
    expect(gutterLines.length).toBeGreaterThanOrEqual(VALID.split("\n").length - 1);
  });

  it("switches between Structure, Preview and Traceability panels", () => {
    render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} />);

    fireEvent.click(screen.getByTestId("markdown-panel-tab-preview"));
    expect(screen.getByTestId("markdown-panel-preview")).toBeInTheDocument();
    expect(screen.getByText("Passwordless auth")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("markdown-panel-tab-traceability"));
    expect(screen.getByTestId("markdown-panel-traceability")).toBeInTheDocument();
    expect(screen.getByTestId("traceability-matrix")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("markdown-panel-tab-structure"));
    expect(screen.getByTestId("markdown-panel-structure")).toBeInTheDocument();
  });

  it("shows line-anchored parse issues for malformed markdown and disables save on invalid content is independent of dirty", () => {
    render(
      <MarkdownMode
        value={"---\nid: SPEC-1\n---\n\n## Requirements\n\n- broken\n"}
        onChange={vi.fn()}
        onSave={vi.fn()}
        dirty
      />,
    );
    expect(screen.getByTestId("markdown-status-invalid")).toBeInTheDocument();
    const issues = screen.getByTestId("markdown-issues");
    expect(issues).toBeInTheDocument();
    expect(screen.getByText(/requirement must be/i)).toBeInTheDocument();
    expect(screen.getByText(/missing a '## Goal' section/i)).toBeInTheDocument();
  });

  it("clicking an issue jumps the textarea cursor to that line", () => {
    const text = "---\nid: SPEC-1\n---\n\n## Goal\n\nX\n\n## Requirements\n\n- broken\n";
    render(<MarkdownMode value={text} onChange={vi.fn()} onSave={vi.fn()} />);
    const textarea = screen.getByTestId("markdown-textarea") as HTMLTextAreaElement;
    const issueButton = screen.getByText(/requirement must be/i).closest("button")!;
    fireEvent.click(issueButton);
    expect(document.activeElement).toBe(textarea);
  });

  it("live-updates the parse status and structure counts as the user types", () => {
    render(<Harness initial={""} />);
    expect(screen.getByTestId("markdown-status-invalid")).toBeInTheDocument();

    const textarea = screen.getByTestId("markdown-textarea");
    fireEvent.change(textarea, { target: { value: VALID } });

    expect(screen.getByTestId("markdown-status-valid")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("markdown-panel-tab-structure"));
    expect(screen.getByText("Requirements")).toBeInTheDocument();
  });

  it("Tab inserts an indent instead of moving focus out of the editor", () => {
    render(<Harness initial={"abc"} />);
    const textarea = screen.getByTestId("markdown-textarea") as HTMLTextAreaElement;
    textarea.focus();
    textarea.setSelectionRange(3, 3);
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(textarea).toHaveValue("abc  ");
  });

  it("enables save once dirty and calls onSave", () => {
    const onSave = vi.fn();
    render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={onSave} dirty />);
    const button = screen.getByTestId("markdown-save");
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(onSave).toHaveBeenCalled();
  });

  it("surfaces a save error when provided", () => {
    render(<MarkdownMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} dirty saveError="409 conflict" />);
    expect(screen.getByTestId("markdown-save-error")).toHaveTextContent("409 conflict");
  });
});
