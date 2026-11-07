import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { THEME_STORAGE_KEY } from "@/lib/theme";

import { ThemeToggle } from "./theme-toggle";

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.style.colorScheme = "";
});

describe("ThemeToggle", () => {
  it("cycles system, light, dark and back, remembering each choice", async () => {
    render(<ThemeToggle />);
    const root = document.documentElement;

    const button = await screen.findByRole("button", {
      name: /theme: system/i,
    });
    fireEvent.click(button);
    expect(
      await screen.findByRole("button", { name: /theme: light/i }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(root).not.toHaveClass("dark");

    fireEvent.click(screen.getByRole("button", { name: /theme: light/i }));
    expect(
      await screen.findByRole("button", { name: /theme: dark/i }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(root).toHaveClass("dark");
    expect(root.style.colorScheme).toBe("dark");

    fireEvent.click(screen.getByRole("button", { name: /theme: dark/i }));
    expect(
      await screen.findByRole("button", { name: /theme: system/i }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    // The test environment's OS preference is light.
    expect(root).not.toHaveClass("dark");
  });

  it("starts from the stored choice", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    render(<ThemeToggle />);
    expect(
      await screen.findByRole("button", { name: /theme: dark/i }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveClass("dark");
  });
});
