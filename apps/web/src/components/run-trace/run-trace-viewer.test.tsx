import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type { RunTrace } from "@/lib/api/types";

import { RunTraceViewer } from "./run-trace-viewer";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const RUN_ID = "11111111-1111-4111-8111-111111111111";

const trace: RunTrace = {
  run_id: RUN_ID,
  status: "succeeded",
  total_steps: 4,
  total_duration_ms: 1025,
  step_counts: { plan: 1, tool_call: 1, decision: 1, output: 1 },
  confidence: 0.82,
  has_subagents: false,
  summary: "4 steps (succeeded)",
  steps: [
    { index: 0, kind: "plan", thought: "Draft the migration plan", duration_ms: 10 },
    {
      index: 1,
      kind: "tool_call",
      thought: "Read the entrypoint",
      tool_call: { tool: "fs.read", path: "src/app.ts", arguments: { encoding: "utf-8" } },
      duration_ms: 120,
      metadata: {
        input_tokens: 1000,
        output_tokens: 200,
        cost_usd: 0.004,
        model: "claude-sonnet",
      },
    },
    {
      index: 2,
      kind: "decision",
      decision: { effect: "deny", reason: "write outside allowed path", severity: "warning" },
      duration_ms: 5,
    },
    { index: 3, kind: "output", output: "Done: opened PR #42", duration_ms: 900 },
  ],
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getRunTrace: vi.fn(() => Promise.resolve(trace)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderViewer(
  props: Partial<React.ComponentProps<typeof RunTraceViewer>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(
    <RunTraceViewer runId={RUN_ID} client={makeClient()} {...props} />,
    { wrapper: Wrapper },
  );
}

function activeStepIndex(): string | null {
  return document
    .querySelector('[aria-current="step"]')
    ?.getAttribute("data-step-index") ?? null;
}

describe("RunTraceViewer", () => {
  it("renders the timeline, status, stats and step titles", async () => {
    renderViewer();
    expect(await screen.findByTestId("run-trace-viewer")).toBeInTheDocument();

    expect(screen.getByRole("heading", { level: 1, name: /run trace/i })).toBeInTheDocument();
    expect(screen.getByText("Succeeded")).toBeInTheDocument();

    // Step titles derived from the trace.
    expect(screen.getByText("Draft the migration plan")).toBeInTheDocument();
    expect(screen.getByText("fs.read · src/app.ts")).toBeInTheDocument();
    expect(screen.getByText(/Denied · write outside allowed path/)).toBeInTheDocument();
    expect(screen.getByText("Done: opened PR #42")).toBeInTheDocument();

    // Rolled-up telemetry (shown on both the stat tile and the step row).
    expect(screen.getAllByText("1.2k").length).toBeGreaterThan(0);
    expect(screen.getAllByText("$0.0040").length).toBeGreaterThan(0);

    // Four steps rendered on the spine.
    expect(screen.getAllByTestId("trace-step")).toHaveLength(4);
  });

  it("expands a step to reveal its detail on click", async () => {
    renderViewer();
    await screen.findByTestId("run-trace-viewer");

    const toolStep = screen.getByText("fs.read · src/app.ts").closest("li");
    expect(toolStep).not.toBeNull();
    fireEvent.click(within(toolStep as HTMLElement).getByRole("button"));

    const detail = await screen.findByTestId("step-detail");
    expect(within(detail).getByText("Read the entrypoint")).toBeInTheDocument();
    // Tool arguments are pretty-printed.
    expect(within(detail).getByText(/encoding/)).toBeInTheDocument();
  });

  it("moves the selection with j and expands the active step with o", async () => {
    renderViewer();
    await screen.findByTestId("run-trace-viewer");
    const app = screen.getByTestId("run-trace-viewer");

    expect(activeStepIndex()).toBe("1");
    fireEvent.keyDown(app, { key: "j" });
    fireEvent.keyDown(app, { key: "j" });
    expect(activeStepIndex()).toBe("3"); // the decision step

    fireEvent.keyDown(app, { key: "o" });
    const detail = await screen.findByTestId("step-detail");
    expect(within(detail).getByText(/write outside allowed path/)).toBeInTheDocument();
  });

  it("replays the run, advancing the playhead to the final step", async () => {
    renderViewer({ replayIntervalMs: 15 });
    await screen.findByTestId("run-trace-viewer");

    const replay = screen.getByTestId("replay-toggle");
    fireEvent.click(replay);
    expect(replay).toHaveAttribute("data-state", "playing");

    await waitFor(() => expect(activeStepIndex()).toBe("4"));
    await waitFor(() =>
      expect(screen.getByTestId("replay-toggle")).toHaveAttribute(
        "data-state",
        "paused",
      ),
    );
  });

  it("shows a loading skeleton while the trace is in flight", () => {
    const client = makeClient({
      getRunTrace: vi.fn(() => new Promise<RunTrace>(() => {})),
    });
    renderViewer({ client });
    expect(screen.getByTestId("trace-skeleton")).toBeInTheDocument();
  });

  it("renders an empty state when the run recorded no steps", async () => {
    const client = makeClient({
      getRunTrace: vi.fn(() =>
        Promise.resolve({ ...trace, steps: [], total_steps: 0, step_counts: {} }),
      ),
    });
    renderViewer({ client });
    expect(await screen.findByTestId("empty-timeline")).toBeInTheDocument();
    expect(screen.getByText(/no steps recorded/i)).toBeInTheDocument();
  });

  it("shows a not-found state for a 404", async () => {
    const client = makeClient({
      getRunTrace: vi.fn(() =>
        Promise.reject(new ApiError(404, "not found", null)),
      ),
    });
    renderViewer({ client });
    expect(await screen.findByTestId("trace-error")).toBeInTheDocument();
    expect(screen.getByText(/run not found/i)).toBeInTheDocument();
  });

  it("offers a retry on a non-404 error", async () => {
    const getRunTrace = vi.fn(() =>
      Promise.reject(new ApiError(500, "server error", null)),
    );
    renderViewer({ client: makeClient({ getRunTrace }) });
    expect(await screen.findByTestId("trace-error")).toBeInTheDocument();
    expect(screen.getByText(/couldn't load the trace/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(getRunTrace).toHaveBeenCalledTimes(2));
  });

  it("shows the entry form and opens a run id (no runId)", async () => {
    const onOpenRun = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>
          <RunTraceViewer client={makeClient()} onOpenRun={onOpenRun} />
        </CommandPaletteProvider>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Run id");
    fireEvent.change(input, { target: { value: "  abc-123  " } });
    fireEvent.click(screen.getByRole("button", { name: /open trace/i }));

    expect(onOpenRun).toHaveBeenCalledWith("abc-123");
  });
});
