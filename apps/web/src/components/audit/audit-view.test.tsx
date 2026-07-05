import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { AuditEntry, AuditListResponse, ChainVerifyResult } from "@/lib/api/types";

import { AuditView } from "./audit-view";
import { REDACTED_PLACEHOLDER } from "./audit-meta";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const ENTRIES: AuditEntry[] = [
  {
    id: "e1",
    workspace_id: "ws-1",
    seq: 2,
    action: "secret.accessed",
    actor_type: "user",
    actor_label: "alice@forge.dev",
    actor_id: "aaaa1111bbbb2222",
    target_type: "mcp_connection",
    target_id: "conn-9999abcd",
    result: "success",
    severity: "warning",
    reason: "read for deploy",
    details: {
      connection: "github",
      api_key: "ghp_supersecretvalue123",
      scope: "repo",
    },
    request_id: "req-1",
    prev_hash: "0".repeat(64),
    entry_hash: "a".repeat(64),
    payload_hash: "b".repeat(64),
    created_at: "2026-07-05T11:00:03Z",
  },
  {
    id: "e2",
    workspace_id: "ws-1",
    seq: 1,
    action: "policy.tool_denied",
    actor_type: "agent_runner",
    actor_id: "agent-777",
    target_type: "task",
    result: "denied",
    severity: "critical",
    details: {},
    created_at: "2026-07-05T10:00:00Z",
  },
];

const VOCAB = {
  actions: ["secret.accessed", "policy.tool_denied", "agent.action"],
  actor_types: ["user", "agent_runner", "system"],
  resource_types: ["task", "mcp_connection"],
  outcomes: ["success", "denied", "error", "blocked"],
  severities: ["info", "notice", "warning", "critical"],
};

const VERDICT: ChainVerifyResult = {
  workspace_id: "ws-1",
  ok: true,
  entries_checked: 2,
  broken_at_seq: null,
  detail: null,
};

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listAudit: vi.fn(() =>
      Promise.resolve<AuditListResponse>({ items: ENTRIES, next_cursor: null }),
    ),
    getAuditVocabulary: vi.fn(() => Promise.resolve(VOCAB)),
    verifyAuditChain: vi.fn(() => Promise.resolve(VERDICT)),
    exportAuditNdjson: vi.fn(() =>
      Promise.resolve(
        `${JSON.stringify(ENTRIES[0])}\n${JSON.stringify(ENTRIES[1])}\n`,
      ),
    ),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
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
  return render(<AuditView client={client} />, { wrapper: Wrapper });
}

describe("AuditView", () => {
  it("renders the filterable table of audit entries", async () => {
    renderView(makeClient());

    expect(await screen.findAllByTestId("audit-row")).toHaveLength(2);
    expect(screen.getByText("alice@forge.dev")).toBeInTheDocument();
    expect(screen.getByText("Tool denied")).toBeInTheDocument();
    expect(screen.getByTestId("audit-count")).toHaveTextContent("2 shown");
    // Outcome + severity badges render.
    expect(screen.getAllByTestId("outcome-badge").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("severity-badge").length).toBeGreaterThan(0);
  });

  it("opens the detail drawer on row click and redacts secrets", async () => {
    renderView(makeClient());
    const rows = await screen.findAllByTestId("audit-row");

    fireEvent.click(rows[0]);

    const dialog = await screen.findByRole("dialog");
    // The full dotted action + integrity note are shown.
    expect(dialog.textContent).toContain("secret.accessed");
    expect(within(dialog).getByText(/hash-chained/i)).toBeInTheDocument();
    // Secret is redacted; the raw credential never renders.
    expect(dialog.textContent).toContain(REDACTED_PLACEHOLDER);
    expect(dialog.textContent).not.toContain("ghp_supersecretvalue123");
    // Non-secret detail survives.
    expect(dialog.textContent).toContain("github");
  });

  it("moves the cursor with j and opens the selected row with Enter", async () => {
    renderView(makeClient());
    await screen.findAllByTestId("audit-row");
    const view = screen.getByTestId("audit-view");

    fireEvent.keyDown(view, { key: "j" });
    fireEvent.keyDown(view, { key: "Enter" });

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/tool denied/i)).toBeInTheDocument();
  });

  it("filters on the server when a filter select changes", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findAllByTestId("audit-row");

    fireEvent.change(screen.getByLabelText("Action"), {
      target: { value: "policy.tool_denied" },
    });

    await waitFor(() =>
      expect(client.listAudit).toHaveBeenCalledWith(
        expect.objectContaining({ action: "policy.tool_denied" }),
      ),
    );
  });

  it("debounces free-text search into the query", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findAllByTestId("audit-row");

    fireEvent.change(screen.getByLabelText("Search audit log"), {
      target: { value: "alice" },
    });

    await waitFor(() =>
      expect(client.listAudit).toHaveBeenCalledWith(
        expect.objectContaining({ q: "alice" }),
      ),
    );
  });

  it("verifies the hash chain and announces the verdict", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findAllByTestId("audit-row");

    fireEvent.click(screen.getByRole("button", { name: /verify chain/i }));

    await waitFor(() => expect(client.verifyAuditChain).toHaveBeenCalled());
    expect(await screen.findByTestId("chain-verdict")).toHaveTextContent(/intact/i);
    expect(screen.getByTestId("audit-status")).toHaveTextContent(/verified/i);
  });

  it("exports the log as NDJSON and announces the row count", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findAllByTestId("audit-row");

    fireEvent.click(screen.getByTestId("export-ndjson"));

    await waitFor(() => expect(client.exportAuditNdjson).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByTestId("audit-status")).toHaveTextContent(
        /Exported 2 entries/i,
      ),
    );
  });

  it("paginates with load-more via the cursor", async () => {
    const client = makeClient({
      listAudit: vi.fn((query?: { cursor?: string }) =>
        Promise.resolve<AuditListResponse>(
          query?.cursor === "c2"
            ? { items: [ENTRIES[1]], next_cursor: null }
            : { items: [ENTRIES[0]], next_cursor: "c2" },
        ),
      ),
    });
    renderView(client);

    await screen.findByTestId("load-more");
    expect(screen.getAllByTestId("audit-row")).toHaveLength(1);

    fireEvent.click(screen.getByTestId("load-more"));

    await waitFor(() =>
      expect(screen.getAllByTestId("audit-row")).toHaveLength(2),
    );
  });

  it("shows the first-run empty state when the log is empty", async () => {
    renderView(
      makeClient({
        listAudit: vi.fn(() =>
          Promise.resolve<AuditListResponse>({ items: [], next_cursor: null }),
        ),
      }),
    );
    expect(await screen.findByTestId("empty-audit")).toBeInTheDocument();
  });

  it("shows the filtered-empty state when active filters match nothing", async () => {
    renderView(
      makeClient({
        listAudit: vi.fn(() =>
          Promise.resolve<AuditListResponse>({ items: [], next_cursor: null }),
        ),
      }),
    );
    await screen.findByTestId("empty-audit");

    fireEvent.change(screen.getByLabelText("Severity"), {
      target: { value: "critical" },
    });

    expect(await screen.findByTestId("empty-filtered")).toBeInTheDocument();
  });

  it("renders a skeleton while the first page is loading", () => {
    renderView(
      makeClient({
        listAudit: vi.fn(() => new Promise<AuditListResponse>(() => {})),
      }),
    );
    expect(screen.getByTestId("audit-skeleton")).toBeInTheDocument();
  });

  it("degrades to an error state when the audit API fails", async () => {
    renderView(
      makeClient({
        listAudit: vi.fn(() => Promise.reject(new Error("offline"))),
      }),
    );
    expect(await screen.findByTestId("audit-error")).toBeInTheDocument();
  });
});
