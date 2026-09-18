import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type { SelfEvalRunAccepted, SelfEvalStatusOut } from "@/lib/api/types";

import { SelfEvalPanel } from "./self-eval-panel";

function makeStatus(over: Partial<SelfEvalStatusOut> = {}): SelfEvalStatusOut {
  return {
    workspace_id: "w-test",
    enforced: true,
    suite: {
      id: "s-1",
      slug: "acme-app-self-eval",
      version: "1.2.0",
      title: "Acme private suite",
      task_count: 12,
      repo_id: "github:acme/app",
      published: true,
    },
    baseline: {
      benchmark_suite_id: "s-1",
      baseline_rate: 0.8,
      resolved: 8,
      total: 10,
      recorded_at: "2026-07-01T12:00:00Z",
    },
    ...over,
  };
}

function makeAccepted(): SelfEvalRunAccepted {
  return {
    status: "queued",
    task: "forge.self_eval.run",
    workspace_id: "w-test",
    benchmark_suite_id: "s-1",
  };
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getSelfEvalStatus: vi.fn(() => Promise.resolve(makeStatus())),
    runSelfEval: vi.fn(() => Promise.resolve(makeAccepted())),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderPanel(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  return render(<SelfEvalPanel client={client} />, { wrapper: Wrapper });
}

describe("SelfEvalPanel", () => {
  it("renders the loading skeleton while the status loads", () => {
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() => new Promise<SelfEvalStatusOut>(() => {})),
      }),
    );
    expect(screen.getByTestId("self-eval-skeleton")).toBeInTheDocument();
  });

  it("shows a fetch error state distinct from the empty (cold-start) state", async () => {
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() =>
          Promise.reject(new ApiError(500, "boom", null)),
        ),
      }),
    );
    expect(await screen.findByTestId("self-eval-error")).toBeInTheDocument();
    expect(screen.queryByTestId("self-eval-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("self-eval-no-baseline")).not.toBeInTheDocument();
  });

  it("renders suite, baseline and last-run facts from the API", async () => {
    renderPanel(makeClient());

    expect(await screen.findByTestId("self-eval-panel")).toBeInTheDocument();
    expect(screen.getByTestId("self-eval-suite")).toHaveTextContent(
      "acme-app-self-eval",
    );
    expect(screen.getByTestId("self-eval-suite")).toHaveTextContent("1.2.0");
    expect(screen.getByTestId("self-eval-suite")).toHaveTextContent(
      "github:acme/app",
    );
    expect(screen.getByTestId("self-eval-baseline")).toHaveTextContent("80.0%");
    expect(screen.getByTestId("self-eval-baseline")).toHaveTextContent("8/10");
    expect(screen.getByTestId("self-eval-last-run")).toHaveTextContent("8/10");
  });

  it("shows the gate as able to block when enforcement is on and a baseline exists", async () => {
    renderPanel(makeClient());
    const gate = await screen.findByTestId("self-eval-gate-status");
    expect(gate).toHaveTextContent(/enforcement on/i);
    // The Phase-A limitation is stated inline, always.
    expect(screen.getByTestId("self-eval-phase-a")).toHaveTextContent(/phase a/i);
  });

  it("shows the gate as off when enforcement is disabled", async () => {
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() =>
          Promise.resolve(makeStatus({ enforced: false })),
        ),
      }),
    );
    expect(await screen.findByTestId("self-eval-gate-status")).toHaveTextContent(
      /enforcement off/i,
    );
  });

  it("states plainly that the gate cannot block until a baseline exists", async () => {
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() =>
          Promise.resolve(makeStatus({ baseline: null })),
        ),
      }),
    );
    const empty = await screen.findByTestId("self-eval-no-baseline");
    expect(empty).toHaveTextContent(/cannot block any config change/i);
    expect(screen.getByTestId("self-eval-last-run")).toHaveTextContent(
      /no scored runs/i,
    );
  });

  it("explains the missing suite on cold start and disables the run action", async () => {
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() =>
          Promise.resolve(makeStatus({ suite: null, baseline: null })),
        ),
      }),
    );
    expect(await screen.findByTestId("self-eval-no-suite")).toHaveTextContent(
      /no private suite/i,
    );
    expect(screen.getByTestId("self-eval-run")).toBeDisabled();
  });

  it("disables the run action for an unpublished suite", async () => {
    const status = makeStatus();
    renderPanel(
      makeClient({
        getSelfEvalStatus: vi.fn(() =>
          Promise.resolve({
            ...status,
            suite: { ...status.suite!, published: false },
          }),
        ),
      }),
    );
    await screen.findByTestId("self-eval-panel");
    expect(screen.getByTestId("self-eval-run")).toBeDisabled();
  });

  it("queues a run and renders the accepted state", async () => {
    const client = makeClient();
    renderPanel(client);
    await screen.findByTestId("self-eval-panel");

    fireEvent.click(screen.getByTestId("self-eval-run"));

    await waitFor(() => expect(client.runSelfEval).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("self-eval-run-accepted")).toHaveTextContent(
      /queued/i,
    );
  });

  it("shows a run error distinct from the accepted state", async () => {
    renderPanel(
      makeClient({
        runSelfEval: vi.fn(() =>
          Promise.reject(new ApiError(409, "no_private_suite", null)),
        ),
      }),
    );
    await screen.findByTestId("self-eval-panel");

    fireEvent.click(screen.getByTestId("self-eval-run"));

    expect(await screen.findByTestId("self-eval-run-error")).toBeInTheDocument();
    expect(screen.queryByTestId("self-eval-run-accepted")).not.toBeInTheDocument();
  });
});
