import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecDashboard as SpecDashboardData } from "@/lib/api/types";

import { SpecDashboard } from "./spec-dashboard";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const dashboard: SpecDashboardData = {
  project_id: "p1",
  constitution: {
    principles: ["Ship the smallest correct change"],
    architecture_guardrails: ["No shared mutable state"],
  },
  specs: [
    {
      id: "s1",
      name: "Passwordless auth",
      status: "clarifying",
      repos: ["forge/api"],
      execution_mode: "single_agent",
      skill_profile: "backend",
      open_questions: [{ id: "Q1", text: "Require MFA?" }],
      decisions: [
        { id: "ADR-1", title: "Use WebAuthn", status: "accepted", decision: "Adopt WebAuthn" },
      ],
      validation: {
        passed: true,
        coverage: 0.86,
        checks: [
          { name: "lint", passed: true },
          { name: "tests", passed: true },
        ],
        traceability: [
          {
            requirement_id: "REQ-1",
            text: "Sign in without a password",
            acceptance_criteria_ids: ["AC-1"],
            task_refs: ["TASK-a1"],
            test_refs: ["test_login"],
            satisfied: true,
          },
        ],
      },
    },
    {
      id: "s2",
      name: "Billing v2",
      status: "validated",
      validation: { passed: true, coverage: 0.9, checks: [], traceability: [] },
    },
  ],
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getProjectSpecOverview: vi.fn(() => Promise.resolve(dashboard)),
    approveSpec: vi.fn((id: string) =>
      Promise.resolve({ id, name: "Passwordless auth", status: "approved" as const }),
    ),
    getSpecManifest: vi.fn((id: string) =>
      Promise.resolve(dashboard.specs.find((s) => s.id === id) ?? dashboard.specs[0]),
    ),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderDashboard(client: ForgeApiClient) {
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
  return render(<SpecDashboard projectId="p1" client={client} />, { wrapper: Wrapper });
}

describe("SpecDashboard", () => {
  it("renders the list and auto-selects the first spec's lifecycle + gates", async () => {
    renderDashboard(makeClient());
    expect(
      await screen.findByRole("heading", { level: 2, name: /passwordless auth/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("lifecycle-stepper")).toBeInTheDocument();
    expect(screen.getByTestId("gate-tiles")).toBeInTheDocument();
    expect(screen.getByTestId("traceability-matrix")).toBeInTheDocument();
    expect(screen.getByText(/2\s*specs/i)).toBeInTheDocument();
  });

  it("moves the spec selection with the j key (keyboard-first)", async () => {
    renderDashboard(makeClient());
    await screen.findByRole("heading", { level: 2, name: /passwordless auth/i });

    fireEvent.keyDown(screen.getByTestId("spec-dashboard"), { key: "j" });

    expect(
      await screen.findByRole("heading", { level: 2, name: /billing v2/i }),
    ).toBeInTheDocument();
    // A validated spec is past the human gate — no approve action offered.
    expect(screen.queryByTestId("approve-spec")).not.toBeInTheDocument();
    expect(screen.getByTestId("selected-status")).toHaveTextContent(/validated/i);
  });

  it("approves the selected spec at the human gate (the one ember action)", async () => {
    const client = makeClient();
    renderDashboard(client);
    await screen.findByRole("heading", { level: 2, name: /passwordless auth/i });

    fireEvent.click(screen.getByTestId("approve-spec"));

    await waitFor(() => expect(client.approveSpec).toHaveBeenCalledWith("s1"));
  });

  it("switches to the manifest and constitution tabs", async () => {
    renderDashboard(makeClient());
    await screen.findByRole("heading", { level: 2, name: /passwordless auth/i });

    fireEvent.click(screen.getByRole("tab", { name: /manifest/i }));
    expect(await screen.findByTestId("manifest-panel")).toBeInTheDocument();
    expect(screen.getByText("forge/api")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /constitution/i }));
    expect(await screen.findByTestId("constitution-panel")).toBeInTheDocument();
    expect(screen.getByText(/smallest correct change/i)).toBeInTheDocument();
  });

  it("switches to the Studio tab and opens Spec Studio for the selected spec", async () => {
    const client = makeClient();
    renderDashboard(client);
    await screen.findByRole("heading", { level: 2, name: /passwordless auth/i });

    fireEvent.click(screen.getByRole("tab", { name: /studio/i }));

    expect(await screen.findByTestId("spec-studio")).toBeInTheDocument();
    expect(await screen.findByTestId("guided-mode")).toBeInTheDocument();
    expect(client.getSpecManifest).toHaveBeenCalledWith("s1");
  });

  it("shows the empty state when the project has no specs", async () => {
    const client = makeClient({
      getProjectSpecOverview: vi.fn(() =>
        Promise.resolve({ project_id: "p1", constitution: null, specs: [] }),
      ),
    });
    renderDashboard(client);
    expect(await screen.findByTestId("empty-specs")).toBeInTheDocument();
    expect(screen.getByText(/no specs yet/i)).toBeInTheDocument();
  });

  it("shows a loading skeleton while specs are in flight", () => {
    const client = makeClient({
      getProjectSpecOverview: vi.fn(() => new Promise<SpecDashboardData>(() => {})),
    });
    renderDashboard(client);
    expect(screen.getByTestId("list-skeleton")).toBeInTheDocument();
  });

  it("degrades gracefully when the spec engine errors", async () => {
    const client = makeClient({
      getProjectSpecOverview: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderDashboard(client);
    expect(await screen.findByTestId("specs-error")).toBeInTheDocument();
    expect(screen.getByText(/live specs are unavailable/i)).toBeInTheDocument();
  });
});
