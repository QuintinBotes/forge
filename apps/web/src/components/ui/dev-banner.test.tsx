import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { DevBanner, DEV_BANNER_DISMISSED_KEY } from "./dev-banner";

beforeEach(() => {
  window.localStorage.clear();
});

describe("DevBanner", () => {
  it("renders the pre-1.0 development notice", () => {
    render(<DevBanner storageKey="test.dev-banner" />);
    const note = screen.getByRole("note", { name: /development status notice/i });
    expect(note).toHaveTextContent(/under active development/i);
    expect(note).toHaveTextContent(/pre-1\.0, not production-ready/i);
  });

  it("links to the repo and release readiness doc", () => {
    render(<DevBanner storageKey="test.dev-banner" />);
    expect(screen.getByRole("link", { name: /repo/i })).toHaveAttribute(
      "href",
      "https://github.com/QuintinBotes/forge",
    );
    expect(screen.getByRole("link", { name: /release readiness/i })).toHaveAttribute(
      "href",
      "https://github.com/QuintinBotes/forge/blob/main/RELEASE_READINESS.md",
    );
  });

  it("dismisses on click and persists the choice to localStorage", () => {
    render(<DevBanner storageKey="test.dev-banner" />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss development status notice/i }));

    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("test.dev-banner")).toBe("1");
  });

  it("stays hidden on a later mount once dismissed was persisted", () => {
    window.localStorage.setItem("test.dev-banner", "1");
    render(<DevBanner storageKey="test.dev-banner" />);

    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("uses the default storage key when none is provided", () => {
    render(<DevBanner />);
    expect(screen.getByRole("note")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /dismiss development status notice/i }));
    expect(window.localStorage.getItem(DEV_BANNER_DISMISSED_KEY)).toBe("1");
  });
});
