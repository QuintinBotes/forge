import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { AttestationOut } from "@/lib/api/types";

import { AttestationPanel } from "./attestation-panel";

const verifiedAttestation: AttestationOut = {
  id: "att-1",
  changeset_hash: "sha256:" + "ab".repeat(32),
  predicate_type: "https://forge.dev/attestations/changeset/v1",
  keyid: "cd".repeat(32),
  payload_hash: "ef".repeat(32),
  created_at: "2026-07-19T00:00:00Z",
  verified: true,
  provenance: {
    workflow_run_id: "wf-1",
    agent_run_id: "ag-1",
    pr_numbers: [7, 9],
    spec_key: "F41",
    spec_version: 2,
    audit_seq: 12,
  },
};

function makeClient(
  getApprovalAttestation: () => Promise<AttestationOut | null>,
): ForgeApiClient {
  return {
    getApprovalAttestation: vi.fn(getApprovalAttestation),
  } as unknown as ForgeApiClient;
}

function renderPanel(approvalId: string | null | undefined, client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  return render(<AttestationPanel approvalId={approvalId} client={client} />, {
    wrapper: Wrapper,
  });
}

describe("AttestationPanel", () => {
  it("renders nothing when no approval id is known", () => {
    const client = makeClient(() => Promise.resolve(null));
    const { container } = renderPanel(null, client);
    expect(container).toBeEmptyDOMElement();
    expect(client.getApprovalAttestation).not.toHaveBeenCalled();
  });

  it("shows an honest absent state when no attestation exists (404)", async () => {
    const client = makeClient(() => Promise.resolve(null));
    renderPanel("a1", client);

    const panel = await screen.findByTestId("attestation-panel");
    expect(panel).toHaveAttribute("data-state", "absent");
    expect(within(panel).getByText(/not attested/i)).toBeInTheDocument();
    expect(client.getApprovalAttestation).toHaveBeenCalledWith("a1");
  });

  it("shows a verified state and reveals provenance on expand", async () => {
    const client = makeClient(() => Promise.resolve(verifiedAttestation));
    renderPanel("a1", client);

    const panel = await screen.findByTestId("attestation-panel");
    expect(panel).toHaveAttribute("data-state", "verified");
    expect(within(panel).getByText(/signature verified/i)).toBeInTheDocument();

    const toggle = within(panel).getByRole("button");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("attestation-details")).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const details = await screen.findByTestId("attestation-details");
    expect(within(details).getByText(verifiedAttestation.changeset_hash)).toBeInTheDocument();
    expect(within(details).getByText(verifiedAttestation.keyid)).toBeInTheDocument();
    expect(within(details).getByText("7, 9")).toBeInTheDocument();
    expect(within(details).getByText("F41 v2")).toBeInTheDocument();
  });

  it("shows an honest failure state when the signature does not verify", async () => {
    const client = makeClient(() =>
      Promise.resolve({ ...verifiedAttestation, verified: false }),
    );
    renderPanel("a1", client);

    const panel = await screen.findByTestId("attestation-panel");
    expect(panel).toHaveAttribute("data-state", "verification-failed");
    expect(
      within(panel).getByText(/signature failed verification/i),
    ).toBeInTheDocument();
    expect(within(panel).queryByText(/signature verified/i)).not.toBeInTheDocument();
  });

  it("renders nothing when the fetch fails (an error is not proof of absence)", async () => {
    const client = makeClient(() => Promise.reject(new Error("network down")));
    const { container } = renderPanel("a1", client);

    await waitFor(() => expect(client.getApprovalAttestation).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
