import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { ReplayRunResult } from "@/lib/api/types";

import { TimeTravelReplay } from "./time-travel-replay";

const RECORDING_ID = "22222222-2222-4222-8222-222222222222";

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return { ...overrides } as unknown as ForgeApiClient;
}

function renderControl(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<TimeTravelReplay runId={RECORDING_ID} client={client} />, {
    wrapper: Wrapper,
  });
}

function reproduced(): ReplayRunResult {
  return {
    run_recording_id: RECORDING_ID,
    diverged: false,
    divergence: null,
    steps: [
      { boundary: "llm", index: 0, matched: true, recorded_digest: "a", replay_digest: "a" },
      {
        boundary: "tool",
        index: 0,
        name: "read_file",
        matched: true,
        recorded_digest: "b",
        replay_digest: "b",
      },
    ],
    result: { status: "succeeded" },
  };
}

function diverged(): ReplayRunResult {
  return {
    run_recording_id: RECORDING_ID,
    diverged: true,
    divergence: { boundary: "llm", index: 0, expected: "a", actual: "z" },
    steps: [
      { boundary: "llm", index: 0, matched: false, recorded_digest: "a", replay_digest: "z" },
      { boundary: "tool", index: 0, name: "read_file", matched: false, replay_digest: null },
    ],
    result: null,
  };
}

describe("TimeTravelReplay", () => {
  it("is collapsed by default and reveals the objective form on toggle", () => {
    renderControl(makeClient());
    expect(screen.queryByTestId("time-travel-panel")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("time-travel-toggle"));
    expect(screen.getByTestId("time-travel-panel")).toBeInTheDocument();
    expect(screen.getByLabelText(/objective that produced this run/i)).toBeInTheDocument();
  });

  it("disables Run until an objective is entered", () => {
    renderControl(makeClient());
    fireEvent.click(screen.getByTestId("time-travel-toggle"));

    const runButton = screen.getByRole("button", { name: /^run$/i });
    expect(runButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/objective that produced this run/i), {
      target: { value: "edit main" },
    });
    expect(runButton).not.toBeDisabled();
  });

  it("shows a success banner when the replay reproduces the tape", async () => {
    const replayRun = vi.fn(() => Promise.resolve(reproduced()));
    renderControl(makeClient({ replayRun }));

    fireEvent.click(screen.getByTestId("time-travel-toggle"));
    fireEvent.change(screen.getByLabelText(/objective that produced this run/i), {
      target: { value: "edit main" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await waitFor(() =>
      expect(replayRun).toHaveBeenCalledWith(RECORDING_ID, { objective: "edit main" }),
    );
    const banner = await screen.findByTestId("replay-result");
    expect(banner).toHaveTextContent(/reproduced the recorded run/i);
    expect(banner).toHaveTextContent("2/2 calls matched");
  });

  it("shows a divergence banner with the first drift location", async () => {
    const replayRun = vi.fn(() => Promise.resolve(diverged()));
    renderControl(makeClient({ replayRun }));

    fireEvent.click(screen.getByTestId("time-travel-toggle"));
    fireEvent.change(screen.getByLabelText(/objective that produced this run/i), {
      target: { value: "a different objective" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    const banner = await screen.findByTestId("replay-result");
    expect(banner).toHaveTextContent(/diverged from the recorded run/i);
    expect(banner).toHaveTextContent("0/2 calls matched");
    expect(banner).toHaveTextContent("first drift at llm call #0");
  });

  it("shows an error message when the replay request fails", async () => {
    const replayRun = vi.fn(() => Promise.reject(new Error("boom")));
    renderControl(makeClient({ replayRun }));

    fireEvent.click(screen.getByTestId("time-travel-toggle"));
    fireEvent.change(screen.getByLabelText(/objective that produced this run/i), {
      target: { value: "edit main" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/couldn't replay this run/i),
    );
  });
});
