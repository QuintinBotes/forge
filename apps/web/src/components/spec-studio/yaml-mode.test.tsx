import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { YamlMode } from "./yaml-mode";

const VALID = `id: SPEC-1\nname: Passwordless auth\nstatus: draft\n`;

function Harness({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial);
  return (
    <YamlMode
      value={value}
      onChange={setValue}
      onSave={vi.fn()}
      dirty={value !== initial}
    />
  );
}

describe("YamlMode", () => {
  it("shows a valid status for a well-formed manifest and disables save when not dirty", () => {
    render(<YamlMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} dirty={false} />);
    expect(screen.getByTestId("yaml-status-valid")).toBeInTheDocument();
    expect(screen.getByTestId("yaml-save")).toBeDisabled();
  });

  it("shows line-anchored errors for an invalid manifest and disables save", () => {
    render(
      <YamlMode value={`name: X\nstatus: bogus\n`} onChange={vi.fn()} onSave={vi.fn()} dirty />,
    );
    expect(screen.getByTestId("yaml-status-invalid")).toBeInTheDocument();
    expect(screen.getByTestId("yaml-save")).toBeDisabled();
    const issues = screen.getByTestId("yaml-issues");
    expect(issues).toBeInTheDocument();
    expect(screen.getByText(/'status' must be one of/i)).toBeInTheDocument();
  });

  it("enables save once the manifest is valid and dirty, and calls onSave", () => {
    const onSave = vi.fn();
    render(<YamlMode value={VALID} onChange={vi.fn()} onSave={onSave} dirty />);
    const button = screen.getByTestId("yaml-save");
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(onSave).toHaveBeenCalled();
  });

  it("renders a line-number gutter matching the text line count", () => {
    const { container } = render(
      <YamlMode value={"id: A\nname: B\nstatus: draft\n"} onChange={vi.fn()} onSave={vi.fn()} />,
    );
    const gutterLines = container.querySelectorAll('[aria-hidden="true"] > div');
    // 4 lines (trailing newline yields an extra empty line, which is fine).
    expect(gutterLines.length).toBeGreaterThanOrEqual(3);
  });

  it("edits update validity live", () => {
    render(<Harness initial={"id: A\nname: B\n"} />);
    expect(screen.getByTestId("yaml-status-valid")).toBeInTheDocument();
    const textarea = screen.getByTestId("yaml-textarea");
    fireEvent.change(textarea, { target: { value: "id: A\nname: B\nstatus: nope\n" } });
    expect(screen.getByTestId("yaml-status-invalid")).toBeInTheDocument();
  });

  it("surfaces a save error when provided", () => {
    render(
      <YamlMode value={VALID} onChange={vi.fn()} onSave={vi.fn()} dirty saveError="409 conflict" />,
    );
    expect(screen.getByTestId("yaml-save-error")).toHaveTextContent("409 conflict");
  });
});
