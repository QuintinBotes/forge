import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CommandAction } from "./command-palette";
import { CommandPaletteProvider } from "./command-palette";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

describe("CommandPaletteProvider", () => {
  it("opens on Cmd+K and runs the selected action", async () => {
    const run = vi.fn();
    const commands: CommandAction[] = [
      { id: "demo", label: "Run demo action", group: "Demo", run },
    ];

    render(
      <CommandPaletteProvider commands={commands}>
        <div>app</div>
      </CommandPaletteProvider>,
    );

    // Closed initially.
    expect(screen.queryByText("Run demo action")).not.toBeInTheDocument();

    // Cmd+K opens the palette.
    fireEvent.keyDown(document, { key: "k", metaKey: true });

    const item = await screen.findByText("Run demo action");
    fireEvent.click(item);

    expect(run).toHaveBeenCalledTimes(1);
    // Palette closes after running an action.
    await waitFor(() =>
      expect(screen.queryByText("Run demo action")).not.toBeInTheDocument(),
    );
  });

  it("toggles closed on a second Cmd+K", async () => {
    render(
      <CommandPaletteProvider commands={[]}>
        <div>app</div>
      </CommandPaletteProvider>,
    );

    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(await screen.findByPlaceholderText(/type a command/i)).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "k", metaKey: true });
    await waitFor(() =>
      expect(screen.queryByPlaceholderText(/type a command/i)).not.toBeInTheDocument(),
    );
  });
});
