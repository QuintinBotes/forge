import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecOverview, TaskDTO } from "@/lib/api/types";

import { LifecycleStepper } from "./lifecycle-stepper";

function renderStepper(spec: SpecOverview, client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<LifecycleStepper spec={spec} client={client} />, { wrapper: Wrapper });
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return { ...overrides } as unknown as ForgeApiClient;
}

describe("LifecycleStepper", () => {
  it("renders all five plain-language steps", () => {
    renderStepper({ id: "s1", name: "Auth", status: "draft" }, makeClient());
    for (const label of ["Describe", "Refine", "Approve", "Build", "Verify"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("marks the current step and offers its action for a fresh draft", () => {
    renderStepper({ id: "s1", name: "Auth", status: "draft" }, makeClient());
    expect(screen.getByTestId("plain-stage-describe")).toHaveAttribute("data-state", "current");
    expect(screen.getByTestId("stepper-run-describe")).toHaveTextContent("Clarify");
  });

  it("runs Clarify and calls the client with the spec id", async () => {
    const clarifySpec = vi.fn(() => Promise.resolve({ id: "s1", name: "Auth", status: "clarifying" as const }));
    const client = makeClient({ clarifySpec });
    renderStepper({ id: "s1", name: "Auth", status: "draft" }, client);

    fireEvent.click(screen.getByTestId("stepper-run-describe"));

    await waitFor(() => expect(clarifySpec).toHaveBeenCalledWith("s1"));
  });

  it("offers Plan once clarifying but before a plan exists", () => {
    renderStepper({ id: "s1", name: "Auth", status: "clarifying" }, makeClient());
    expect(screen.getByTestId("plain-stage-refine")).toHaveAttribute("data-state", "current");
    expect(screen.getByTestId("stepper-run-refine")).toHaveTextContent("Plan");
  });

  it("offers Generate tasks once approved with a plan", () => {
    renderStepper(
      { id: "s1", name: "Auth", status: "approved", plan_ref: "plan.md" },
      makeClient(),
    );
    expect(screen.getByTestId("plain-stage-build")).toHaveAttribute("data-state", "current");
    expect(screen.getByTestId("stepper-run-build")).toHaveTextContent("Generate tasks");
  });

  it("offers Validate once tasks are generated, chaining generateTasks + validateTask", async () => {
    const tasks: TaskDTO[] = [{ id: "t1", title: "Implement R1" }];
    const generateTasks = vi.fn(() => Promise.resolve(tasks));
    const validateTask = vi.fn(() => Promise.resolve({ task_id: "t1", passed: true }));
    const client = makeClient({ generateTasks, validateTask });
    renderStepper(
      {
        id: "s1",
        name: "Auth",
        status: "approved",
        plan_ref: "plan.md",
        tasks_ref: "tasks.md",
      },
      client,
    );

    expect(screen.getByTestId("plain-stage-verify")).toHaveAttribute("data-state", "current");
    fireEvent.click(screen.getByTestId("stepper-run-verify"));

    await waitFor(() => expect(validateTask).toHaveBeenCalledWith("t1"));
    expect(generateTasks).toHaveBeenCalledWith("s1");
  });

  it("shows a completion message once every step is done", () => {
    renderStepper(
      {
        id: "s1",
        name: "Auth",
        status: "validated",
        plan_ref: "plan.md",
        tasks_ref: "tasks.md",
      },
      makeClient(),
    );
    expect(screen.getByTestId("stepper-complete")).toBeInTheDocument();
    expect(screen.queryByTestId(/stepper-run-/)).not.toBeInTheDocument();
  });

  it("surfaces a mutation error inline", async () => {
    const clarifySpec = vi.fn(() => Promise.reject(new Error("engine offline")));
    const client = makeClient({ clarifySpec });
    renderStepper({ id: "s1", name: "Auth", status: "draft" }, client);

    fireEvent.click(screen.getByTestId("stepper-run-describe"));

    expect(await screen.findByTestId("stepper-error")).toHaveTextContent(/engine offline/i);
  });
});
