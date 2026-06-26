import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppShell, BOARD_NAV } from "./app-shell";

describe("AppShell", () => {
  it("renders the Forge brand", () => {
    render(<AppShell>content</AppShell>);
    expect(screen.getByText("Forge")).toBeInTheDocument();
  });

  it("renders every primary board navigation item", () => {
    render(<AppShell>content</AppShell>);
    const nav = screen.getByRole("navigation", { name: /primary/i });
    for (const item of BOARD_NAV) {
      expect(nav).toHaveTextContent(item.label);
    }
  });

  it("renders its children in the main content region", () => {
    render(
      <AppShell>
        <p>hello board</p>
      </AppShell>,
    );
    const main = screen.getByRole("main");
    expect(main).toHaveTextContent("hello board");
  });

  it("shows the command-palette keyboard hint", () => {
    render(<AppShell>content</AppShell>);
    expect(screen.getByText("⌘K")).toBeInTheDocument();
  });
});
