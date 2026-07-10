import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import SettingsLayout, { SETTINGS_SECTIONS } from "./layout";

let mockPathname: string | null = "/settings/models";
vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
}));

describe("SettingsLayout", () => {
  it("renders every settings section as a link", () => {
    render(<SettingsLayout>content</SettingsLayout>);
    const nav = screen.getByRole("navigation", { name: /settings sections/i });
    for (const section of SETTINGS_SECTIONS) {
      expect(
        screen.getByRole("link", { name: section.label }),
      ).toHaveAttribute("href", section.href);
    }
    expect(nav).toBeInTheDocument();
  });

  it("marks the current section active and others not", () => {
    mockPathname = "/settings/models";
    render(<SettingsLayout>content</SettingsLayout>);
    expect(screen.getByRole("link", { name: /models & effort/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: /^access$/i })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("treats nested settings routes as active", () => {
    mockPathname = "/settings/rbac/teams";
    render(<SettingsLayout>content</SettingsLayout>);
    expect(screen.getByRole("link", { name: /^access$/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("renders its children below the nav", () => {
    mockPathname = "/settings/models";
    render(
      <SettingsLayout>
        <p>settings content</p>
      </SettingsLayout>,
    );
    expect(screen.getByText("settings content")).toBeInTheDocument();
  });
});
