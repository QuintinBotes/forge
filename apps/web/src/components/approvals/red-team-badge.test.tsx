import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { RedTeamGateOut } from "@/lib/api/types";

import { RedTeamBadge } from "./red-team-badge";

function makeClient(getWorkflowRunRedTeam: () => Promise<RedTeamGateOut>): ForgeApiClient {
  return { getWorkflowRunRedTeam: vi.fn(getWorkflowRunRedTeam) } as unknown as ForgeApiClient;
}

function renderBadge(workflowRunId: string | null | undefined, client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  return render(<RedTeamBadge workflowRunId={workflowRunId} client={client} />, {
    wrapper: Wrapper,
  });
}

describe("RedTeamBadge", () => {
  it("renders nothing when no workflow run id is known", () => {
    const client = makeClient(() =>
      Promise.resolve({ workflow_run_id: "wf-1", latest: null, records: [] }),
    );
    const { container } = renderBadge(null, client);
    expect(container).toBeEmptyDOMElement();
    expect(client.getWorkflowRunRedTeam).not.toHaveBeenCalled();
  });

  it("renders nothing while the run has not been scanned yet", async () => {
    const client = makeClient(() =>
      Promise.resolve({ workflow_run_id: "wf-1", latest: null, records: [] }),
    );
    const { container } = renderBadge("wf-1", client);
    await waitFor(() => expect(client.getWorkflowRunRedTeam).toHaveBeenCalledWith("wf-1"));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a success badge for a survived verdict", async () => {
    const client = makeClient(() =>
      Promise.resolve({
        workflow_run_id: "wf-1",
        latest: {
          id: "rt-1",
          verdict: "survived",
          kind: "failing_test",
          evidence: { ran: true, failed: false },
          adversary_model: "gpt-5-heavy",
          coder_model: "claude-sonnet-4",
          created_at: "2026-07-08T00:00:00Z",
        },
        records: [],
      }),
    );
    renderBadge("wf-1", client);

    const badge = await screen.findByTestId("red-team-badge");
    expect(badge).toHaveAttribute("data-verdict", "survived");
    expect(within(badge).getByText(/survived adversarial review/i)).toBeInTheDocument();
  });

  it("shows a danger badge for a blocked verdict and reveals evidence on expand", async () => {
    const client = makeClient(() =>
      Promise.resolve({
        workflow_run_id: "wf-1",
        latest: {
          id: "rt-2",
          verdict: "blocked",
          kind: "failing_test",
          evidence: { test: "test_boom", stdout: "AssertionError" },
          adversary_model: "gpt-5-heavy",
          coder_model: "claude-sonnet-4",
          created_at: "2026-07-08T00:00:00Z",
        },
        records: [],
      }),
    );
    renderBadge("wf-1", client);

    const badge = await screen.findByTestId("red-team-badge");
    expect(badge).toHaveAttribute("data-verdict", "blocked");
    const toggle = within(badge).getByRole("button");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("red-team-evidence")).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const evidence = await screen.findByTestId("red-team-evidence");
    expect(within(evidence).getByText("AssertionError")).toBeInTheDocument();
    expect(within(evidence).getByText("test_boom")).toBeInTheDocument();
  });
});
