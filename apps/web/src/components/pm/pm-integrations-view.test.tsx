import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  PmConnection,
  PmConnectionDetail,
  PmLink,
} from "@/lib/api/types";

import { PmIntegrationsView } from "./pm-integrations-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/settings/integrations",
}));

function makeConn(over: Partial<PmConnection> = {}): PmConnection {
  return {
    id: "conn-1",
    provider: "jira",
    name: "Platform",
    project_id: "proj-1",
    external_base_url: "https://acme.atlassian.net",
    external_project_key: "ENG",
    external_project_id: "10001",
    auth_type: "api_token",
    account_label: "bot@acme.com",
    granted_scopes: ["read:jira-work"],
    sync_direction: "bidirectional",
    conflict_policy: "newest_wins",
    status_map: { "In Progress": "started" },
    priority_map: {},
    field_map: {},
    status: "connected",
    last_health_at: "2026-07-05T11:00:00Z",
    last_full_sync_at: "2026-07-05T10:00:00Z",
    has_credential: true,
    has_webhook_secret: true,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-05T11:00:00Z",
    ...over,
  };
}

function makeDetail(over: Partial<PmConnectionDetail> = {}): PmConnectionDetail {
  return {
    ...makeConn(over),
    link_counts: { synced: 4, conflict: 1, pending_out: 2 },
    ...over,
  };
}

const CONN_1 = makeConn();
const CONN_2 = makeConn({
  id: "conn-2",
  provider: "linear",
  name: "Growth",
  external_project_key: "GRO",
  status: "error",
});

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    baseUrl: "http://localhost:8000",
    listPmConnections: vi.fn(() => Promise.resolve([CONN_1, CONN_2])),
    getPmConnection: vi.fn((id: string) =>
      Promise.resolve(
        id === "conn-2"
          ? makeDetail({
              id: "conn-2",
              provider: "linear",
              name: "Growth",
              external_project_key: "GRO",
              status: "error",
              status_map: {},
              link_counts: { error: 3 },
            })
          : makeDetail(),
      ),
    ),
    listPmLinks: vi.fn(() => Promise.resolve([] as PmLink[])),
    createPmConnection: vi.fn((body) =>
      Promise.resolve(makeConn({ id: "conn-new", ...body })),
    ),
    patchPmConnection: vi.fn((id: string, body) =>
      Promise.resolve(makeConn({ id, ...body })),
    ),
    disconnectPmConnection: vi.fn(() =>
      Promise.resolve(makeConn({ status: "disabled" })),
    ),
    testPmConnection: vi.fn(() =>
      Promise.resolve({
        status: "connected" as const,
        provider: "jira" as const,
        latency_ms: 42,
        account: "bot@acme.com",
        granted_scopes: ["read:jira-work"],
        error: null,
      }),
    ),
    ...overrides,
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
  return render(<PmIntegrationsView client={client} />, { wrapper: Wrapper });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("PmIntegrationsView — top-level states", () => {
  it("renders the loading skeleton while connections load", () => {
    renderView(
      makeClient({
        listPmConnections: vi.fn(() => new Promise<PmConnection[]>(() => {})),
      }),
    );
    expect(screen.getByTestId("pm-skeleton")).toBeInTheDocument();
  });

  it("shows the error state and retries", async () => {
    const refetchable = makeClient({
      listPmConnections: vi.fn(() =>
        Promise.reject(new ApiError(500, "boom", null)),
      ),
    });
    renderView(refetchable);
    expect(await screen.findByTestId("pm-error")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(refetchable.listPmConnections).toHaveBeenCalledTimes(2),
    );
  });

  it("guides to the first connection when none exist", async () => {
    renderView(makeClient({ listPmConnections: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("pm-empty")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("pm-empty-connect"));
    expect(await screen.findByTestId("pm-form")).toBeInTheDocument();
  });
});

describe("PmIntegrationsView — detail", () => {
  it("renders the rail and the first connection's detail with health counts", async () => {
    renderView(makeClient());

    expect(await screen.findByTestId("pm-detail")).toBeInTheDocument();
    // Rail lists both connections.
    expect(screen.getByTestId("pm-conn-conn-1")).toBeInTheDocument();
    expect(screen.getByTestId("pm-conn-conn-2")).toBeInTheDocument();
    // Header + status.
    expect(screen.getByTestId("pm-status-badge")).toHaveTextContent(/connected/i);
    // Health strip reflects link_counts.
    expect(screen.getByTestId("pm-health-synced")).toHaveTextContent("4");
    expect(screen.getByTestId("pm-health-conflict")).toHaveTextContent("1");
    expect(screen.getByTestId("pm-health-error")).toHaveTextContent("0");
    // No conflicts by default → inbox-zero empty state.
    expect(await screen.findByTestId("pm-conflicts-empty")).toBeInTheDocument();
  });

  it("switches to another connection when its rail item is clicked", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-conn-conn-2"));

    await waitFor(() =>
      expect(client.getPmConnection).toHaveBeenCalledWith("conn-2"),
    );
    const detail = await screen.findByTestId("pm-detail");
    expect(within(detail).getByText("Growth")).toBeInTheDocument();
    expect(within(detail).getByTestId("pm-status-badge")).toHaveTextContent(
      /error/i,
    );
  });

  it("runs the health probe and shows the latency verdict", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-test"));

    await waitFor(() =>
      expect(client.testPmConnection).toHaveBeenCalledWith("conn-1"),
    );
    expect(await screen.findByTestId("pm-health-result")).toHaveTextContent(
      /connected in 42ms/i,
    );
  });

  it("surfaces a failed health probe", async () => {
    const client = makeClient({
      testPmConnection: vi.fn(() => Promise.reject(new Error("unreachable"))),
    });
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-test"));

    expect(await screen.findByTestId("pm-health-error")).toBeInTheDocument();
  });

  it("edits a status mapping and saves it", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    // The seeded row maps "In Progress" → In progress; recategorize it to Done.
    fireEvent.change(screen.getByLabelText("Forge category 1"), {
      target: { value: "completed" },
    });
    fireEvent.click(screen.getByTestId("pm-map-save"));

    await waitFor(() =>
      expect(client.patchPmConnection).toHaveBeenCalledWith("conn-1", {
        status_map: { "In Progress": "completed" },
      }),
    );
  });

  it("adds a new mapping row and includes it on save", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-map-add"));
    fireEvent.change(screen.getByLabelText("Provider status 2"), {
      target: { value: "Blocked" },
    });
    fireEvent.change(screen.getByLabelText("Forge category 2"), {
      target: { value: "canceled" },
    });
    fireEvent.click(screen.getByTestId("pm-map-save"));

    await waitFor(() =>
      expect(client.patchPmConnection).toHaveBeenCalledWith("conn-1", {
        status_map: { "In Progress": "started", Blocked: "canceled" },
      }),
    );
  });

  it("changes the conflict policy inline", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.change(screen.getByLabelText("Conflict policy"), {
      target: { value: "manual" },
    });

    await waitFor(() =>
      expect(client.patchPmConnection).toHaveBeenCalledWith("conn-1", {
        conflict_policy: "manual",
      }),
    );
  });

  it("pauses sync via the enable toggle", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByRole("switch", { name: /sync enabled/i }));

    await waitFor(() =>
      expect(client.patchPmConnection).toHaveBeenCalledWith("conn-1", {
        enabled: false,
      }),
    );
  });

  it("disconnects a connection", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-disconnect"));

    await waitFor(() =>
      expect(client.disconnectPmConnection).toHaveBeenCalledWith("conn-1"),
    );
  });
});

describe("PmIntegrationsView — conflict inbox", () => {
  const CONFLICT: PmLink = {
    id: "link-1",
    forge_task_id: "abcdef12-3456-7890-abcd-ef1234567890",
    provider: "jira",
    external_id: "10023",
    external_key: "ENG-42",
    external_url: "https://acme.atlassian.net/browse/ENG-42",
    sync_state: "conflict",
    last_synced_at: "2026-07-05T09:00:00Z",
    conflict_detail: { title: "clash", status: "clash" },
  };

  it("lists open conflicts with a link out to the provider", async () => {
    const client = makeClient({
      listPmLinks: vi.fn(() => Promise.resolve([CONFLICT])),
    });
    renderView(client);

    const row = await screen.findByTestId("pm-conflict-row");
    expect(row).toHaveTextContent("ENG-42");
    expect(within(row).getByRole("link", { name: /open in jira/i })).toHaveAttribute(
      "href",
      "https://acme.atlassian.net/browse/ENG-42",
    );
    // Only the conflict-scoped links are requested.
    expect(client.listPmLinks).toHaveBeenCalledWith("conn-1", "conflict");
  });

  it("shows the conflicts error state", async () => {
    const client = makeClient({
      listPmLinks: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
    });
    renderView(client);

    expect(await screen.findByTestId("pm-conflicts-error")).toBeInTheDocument();
  });
});

describe("PmIntegrationsView — connect form", () => {
  it("opens the form from the header and cancels back to detail", async () => {
    renderView(makeClient());
    await screen.findByTestId("pm-detail");

    fireEvent.click(screen.getByTestId("pm-new-open"));
    expect(await screen.findByTestId("pm-form")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("pm-form-cancel"));
    expect(await screen.findByTestId("pm-detail")).toBeInTheDocument();
  });

  it("keeps Connect disabled until required fields are filled", async () => {
    renderView(makeClient({ listPmConnections: vi.fn(() => Promise.resolve([])) }));
    fireEvent.click(await screen.findByTestId("pm-empty-connect"));

    expect(await screen.findByTestId("pm-connect")).toBeDisabled();
  });

  it("connects a Jira project with an API token", async () => {
    const client = makeClient({
      listPmConnections: vi.fn(() => Promise.resolve([])),
    });
    renderView(client);
    fireEvent.click(await screen.findByTestId("pm-empty-connect"));
    await screen.findByTestId("pm-form");

    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Platform" },
    });
    fireEvent.change(screen.getByLabelText("External project key"), {
      target: { value: "ENG" },
    });
    fireEvent.change(screen.getByLabelText("Forge project"), {
      target: { value: "proj-1" },
    });
    fireEvent.change(screen.getByLabelText("Jira site URL"), {
      target: { value: "https://acme.atlassian.net" },
    });
    fireEvent.change(screen.getByLabelText("API token"), {
      target: { value: "secret-token" },
    });
    fireEvent.change(screen.getByLabelText("Account email"), {
      target: { value: "bot@acme.com" },
    });

    fireEvent.click(screen.getByTestId("pm-connect"));

    await waitFor(() =>
      expect(client.createPmConnection).toHaveBeenCalledWith(
        expect.objectContaining({
          provider: "jira",
          name: "Platform",
          project_id: "proj-1",
          external_project_key: "ENG",
          external_base_url: "https://acme.atlassian.net",
          auth_type: "api_token",
          api_token: "secret-token",
          api_token_email: "bot@acme.com",
        }),
      ),
    );
  });

  it("hides Jira-only fields when Linear is selected", async () => {
    renderView(makeClient({ listPmConnections: vi.fn(() => Promise.resolve([])) }));
    fireEvent.click(await screen.findByTestId("pm-empty-connect"));
    await screen.findByTestId("pm-form");

    fireEvent.click(screen.getByTestId("pm-provider-linear"));

    expect(screen.queryByLabelText("Jira site URL")).not.toBeInTheDocument();
    // API token stays (still api_token auth), but the Jira email is gone.
    expect(screen.queryByLabelText("Account email")).not.toBeInTheDocument();
    expect(screen.getByLabelText("API token")).toBeInTheDocument();
  });

  it("swaps the token fields for an OAuth note", async () => {
    renderView(makeClient({ listPmConnections: vi.fn(() => Promise.resolve([])) }));
    fireEvent.click(await screen.findByTestId("pm-empty-connect"));
    await screen.findByTestId("pm-form");

    fireEvent.click(screen.getByTestId("pm-auth-oauth"));

    expect(screen.getByTestId("pm-oauth-note")).toBeInTheDocument();
    expect(screen.queryByLabelText("API token")).not.toBeInTheDocument();
  });
});
