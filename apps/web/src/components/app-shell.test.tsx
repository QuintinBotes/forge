import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell, BOARD_NAV, NAV_SECTIONS } from "./app-shell";

let mockPathname: string | null = "/board";
vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
}));

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

  it("groups navigation under labelled sections", () => {
    render(<AppShell>content</AppShell>);
    for (const section of NAV_SECTIONS) {
      expect(
        screen.getByRole("heading", { name: section.label, level: 2 }),
      ).toBeInTheDocument();
    }
  });

  it("marks the current route as active and others not", () => {
    mockPathname = "/board";
    render(<AppShell>content</AppShell>);
    expect(screen.getByRole("link", { name: /^board$/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: /^list$/i })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("exposes a skip-to-content link targeting the main region", () => {
    render(<AppShell>content</AppShell>);
    const skip = screen.getByRole("link", { name: /skip to content/i });
    expect(skip).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
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

  it("offers a control to open navigation on small screens", () => {
    render(<AppShell>content</AppShell>);
    expect(
      screen.getByRole("button", { name: /open navigation/i }),
    ).toBeInTheDocument();
  });
});
