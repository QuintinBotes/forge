import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecVersionDiff, SpecVersionSummary } from "@/lib/api/types";

import { VersionHistory } from "./version-history";

function renderHistory(client: ForgeApiClient, specId = "spec-uuid-1") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<VersionHistory specId={specId} client={client} />, { wrapper: Wrapper });
}

const versions: SpecVersionSummary[] = [
  {
    version_number: 2,
    name: "Passwordless auth v2",
    status: "draft",
    created_at: "2026-07-09T12:00:00Z",
    created_by: "user-1",
  },
  {
    version_number: 1,
    name: "Passwordless auth",
    status: "draft",
    created_at: "2026-07-08T12:00:00Z",
    created_by: "user-1",
  },
];

const diff: SpecVersionDiff = {
  from_version: 1,
  to_version: 2,
  markdown: [
    { op: "equal", text: "## Goal" },
    { op: "delete", text: "Sign in" },
    { op: "insert", text: "Sign in without a password" },
  ],
  manifest: {
    scalar_changes: [{ field: "name", before: "Passwordless auth", after: "Passwordless auth v2" }],
    requirements: [
      { id: "R2", change: "added", after: { id: "R2", text: "Support magic links" } },
    ],
    acceptance_criteria: [],
    open_questions: [],
    decisions: [],
    constraints_added: [],
    constraints_removed: [],
  },
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listSpecVersions: vi.fn(() => Promise.resolve(versions)),
    diffSpecVersions: vi.fn(() => Promise.resolve(diff)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

describe("VersionHistory", () => {
  it("lists versions newest first", async () => {
    renderHistory(makeClient());
    expect(await screen.findByTestId("version-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("version-row-1")).toBeInTheDocument();
  });

  it("defaults the diff to the two most recent versions and renders the markdown + manifest diff", async () => {
    const client = makeClient();
    renderHistory(client);
    await screen.findByTestId("version-row-2");

    await waitFor(() => expect(client.diffSpecVersions).toHaveBeenCalledWith("spec-uuid-1", 1, 2));

    expect(await screen.findByTestId("diff-markdown")).toHaveTextContent(
      "Sign in without a password",
    );
    expect(screen.getByTestId("diff-field-requirements")).toHaveTextContent("added");
    expect(screen.getAllByText(/Passwordless auth v2/).length).toBeGreaterThan(0);
  });

  it("shows an empty state when the spec has no versions yet", async () => {
    renderHistory(makeClient({ listSpecVersions: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("version-history-empty")).toBeInTheDocument();
  });

  it("re-diffs when the compared versions change", async () => {
    const client = makeClient();
    renderHistory(client);
    await screen.findByTestId("version-row-2");
    await waitFor(() => expect(client.diffSpecVersions).toHaveBeenCalledWith("spec-uuid-1", 1, 2));

    fireEvent.change(screen.getByTestId("diff-from-select"), { target: { value: "2" } });

    await waitFor(() => expect(client.diffSpecVersions).toHaveBeenCalledWith("spec-uuid-1", 2, 2));
  });
});
