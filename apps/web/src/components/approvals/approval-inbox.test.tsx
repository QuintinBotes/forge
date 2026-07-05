import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type {
  ApprovalContext,
  ApprovalSummary,
} from "@/lib/api/types";

import { ApprovalInbox } from "./approval-inbox";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const gates: ApprovalSummary[] = [
  {
    id: "a1",
    gate_type: "pr",
    status: "pending",
    title: "Merge auth refactor",
    risk_level: "warning",
    requested_actor: "agent:1111",
    requested_at: "2026-07-05T11:00:00Z",
  },
  {
    id: "a2",
    gate_type: "deploy",
    status: "pending",
    title: "Promote to production",
    risk_level: "critical",
    requested_actor: "system",
    requested_at: "2026-07-05T10:00:00Z",
  },
];

function contextFor(id: string): ApprovalContext {
  return {
    approval_id: id,
    gate_type: "pr",
    goal: "Ship the passwordless auth refactor",
    risk_flags: [],
    available_actions: ["approve", "reject", "request_changes", "escalate"],
  };
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listApprovals: vi.fn(() => Promise.resolve(gates)),
    approvalCount: vi.fn(() => Promise.resolve({ count: gates.length })),
    getApprovalContext: vi.fn((id: string) => Promise.resolve(contextFor(id))),
    listApprovalDecisions: vi.fn(() => Promise.resolve([])),
    decideApproval: vi.fn((id: string) =>
      Promise.resolve({
        approval_id: id,
        status: "approved" as const,
        outcome: { completed: true },
      }),
    ),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderInbox(client: ForgeApiClient) {
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
  return render(<ApprovalInbox client={client} />, { wrapper: Wrapper });
}

describe("ApprovalInbox", () => {
  it("renders the queue and auto-selects the first gate's review panel", async () => {
    renderInbox(makeClient());
    expect(await screen.findByTestId("review-panel")).toBeInTheDocument();
    // The detail header (h2) reflects the first, highest-risk gate.
    expect(
      screen.getByRole("heading", { level: 2, name: /merge auth refactor/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("pending-count")).toHaveTextContent("2 pending");
  });

  it("moves the selection with the j key (keyboard-first)", async () => {
    renderInbox(makeClient());
    await screen.findByTestId("review-panel");

    fireEvent.keyDown(screen.getByTestId("approval-inbox"), { key: "j" });

    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: /promote to production/i,
      }),
    ).toBeInTheDocument();
  });

  it("approves the selected gate with the 'a' shortcut", async () => {
    const client = makeClient();
    renderInbox(client);
    await screen.findByTestId("review-panel");

    fireEvent.keyDown(screen.getByTestId("approval-inbox"), { key: "a" });

    await waitFor(() =>
      expect(client.decideApproval).toHaveBeenCalledWith("a1", {
        decision: "approve",
        note: null,
      }),
    );
  });

  it("opens the reason composer on 'x' and rejects with a note", async () => {
    const client = makeClient();
    renderInbox(client);
    await screen.findByTestId("review-panel");

    fireEvent.keyDown(screen.getByTestId("approval-inbox"), { key: "x" });

    const textarea = await screen.findByLabelText(/reason for rejecting/i);
    fireEvent.change(textarea, { target: { value: "credential store risk" } });
    fireEvent.click(screen.getByTestId("confirm-decision"));

    await waitFor(() =>
      expect(client.decideApproval).toHaveBeenCalledWith("a1", {
        decision: "reject",
        note: "credential store risk",
      }),
    );
  });

  it("filters to the reviewer's own gates via the toggle", async () => {
    const client = makeClient();
    renderInbox(client);
    await screen.findByTestId("review-panel");

    fireEvent.click(screen.getByRole("button", { name: /assigned to me/i }));

    await waitFor(() =>
      expect(client.listApprovals).toHaveBeenCalledWith({
        status: "pending",
        mine: true,
      }),
    );
  });

  it("shows the empty state when the queue is clear", async () => {
    const client = makeClient({
      listApprovals: vi.fn(() => Promise.resolve([])),
    });
    renderInbox(client);
    expect(await screen.findByTestId("empty-queue")).toBeInTheDocument();
    expect(screen.getByText(/queue is clear/i)).toBeInTheDocument();
  });

  it("degrades gracefully when the approvals API errors", async () => {
    const client = makeClient({
      listApprovals: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderInbox(client);
    expect(
      await screen.findByText(/live approvals are unavailable/i),
    ).toBeInTheDocument();
  });
});
