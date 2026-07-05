import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  IncidentDetailView,
  IncidentEventView,
  IncidentView,
  PostmortemView,
  RemediationPlanView,
} from "@/lib/api/types";

import { IncidentsView } from "./incidents-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const incidents: IncidentView[] = [
  {
    id: "i1",
    key: "INC-1",
    project_id: "proj-1",
    title: "Checkout latency spike",
    severity: "critical",
    state: "monitoring",
    lifecycle_state: "monitoring",
    source: "manual",
    blast_radius: "high",
    created_at: "2026-07-05T11:00:00Z",
    allowed_events: ["recovery_confirmed", "recovery_failed"],
  },
  {
    id: "i2",
    key: "INC-2",
    project_id: "proj-1",
    title: "Auth token rotation failing",
    severity: "high",
    state: "incident_created",
    lifecycle_state: "incident_created",
    source: "manual",
    created_at: "2026-07-05T10:00:00Z",
    allowed_events: ["incident_acknowledged", "cancel"],
  },
];

const plan: RemediationPlanView = {
  id: "p1",
  incident_id: "i1",
  attempt: 1,
  max_blast_radius: "high",
  status: "proposed",
  offending_step_ids: ["s1"],
  steps: [
    {
      id: "s1",
      order: 1,
      title: "Flush the global cache",
      action: "cache.flush_all",
      blast_radius: "high",
      rationale: "Poisoned entries suspected.",
      status: "proposed",
      blocked: true,
    },
  ],
};

const postmortem: PostmortemView = {
  id: "pm1",
  incident_id: "i1",
  status: "draft",
  content_md: "# Summary\nLatency spiked after a deploy.",
  root_cause: "Connection pool exhaustion.",
  action_item_task_keys: ["ENG-201"],
};

function detailFor(id: string): IncidentDetailView {
  const base = incidents.find((i) => i.id === id) ?? incidents[0];
  return { ...base, remediation_plan: id === "i1" ? plan : null, event_count: 3 };
}

function timelineFor(id: string): IncidentEventView[] {
  return [
    {
      id: `${id}-e1`,
      incident_id: id,
      sequence: 1,
      kind: "state_change",
      actor: "user:abcdef12",
      summary: "incident declared (manual)",
      data: {},
      created_at: "2026-07-05T11:00:00Z",
    },
  ];
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listIncidentRecords: vi.fn(() => Promise.resolve(incidents)),
    getIncident: vi.fn((id: string) => Promise.resolve(detailFor(id))),
    getIncidentTimeline: vi.fn((id: string) => Promise.resolve(timelineFor(id))),
    getRemediationPlan: vi.fn((id: string) =>
      id === "i1"
        ? Promise.resolve(plan)
        : Promise.reject(new ApiError(404, "no plan", null)),
    ),
    getPostmortem: vi.fn((id: string) =>
      id === "i1"
        ? Promise.resolve(postmortem)
        : Promise.reject(new ApiError(404, "no postmortem", null)),
    ),
    sendIncidentEvent: vi.fn((id: string) => Promise.resolve(detailFor(id))),
    publishPostmortem: vi.fn(() =>
      Promise.resolve({ ...postmortem, status: "published" }),
    ),
    declareIncident: vi.fn((body) =>
      Promise.resolve({
        ...incidents[0],
        id: "i9",
        key: "INC-9",
        title: body.title,
      } as IncidentView),
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
  return render(<IncidentsView client={client} />, { wrapper: Wrapper });
}

describe("IncidentsView", () => {
  it("renders the queue and auto-selects the top (critical) incident", async () => {
    renderView(makeClient());
    expect(await screen.findByTestId("incident-detail")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: /checkout latency spike/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("open-count")).toHaveTextContent("2 open");
    // Lifecycle + blast-radius badges are present in the header.
    expect(screen.getAllByTestId("lifecycle-badge").length).toBeGreaterThan(0);
    expect(screen.getByTestId("blast-radius-badge")).toBeInTheDocument();
  });

  it("moves the selection with the j key (keyboard-first)", async () => {
    renderView(makeClient());
    await screen.findByTestId("incident-detail");

    fireEvent.keyDown(screen.getByTestId("incidents-view"), { key: "j" });

    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: /auth token rotation failing/i,
      }),
    ).toBeInTheDocument();
  });

  it("drives the FSM by sending an allowed event from the action bar", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("incident-detail");

    fireEvent.click(await screen.findByRole("button", { name: /confirm recovery/i }));

    await waitFor(() =>
      expect(client.sendIncidentEvent).toHaveBeenCalledWith("i1", {
        event: "recovery_confirmed",
      }),
    );
  });

  it("opens the declare dialog with the 'c' shortcut", async () => {
    renderView(makeClient());
    await screen.findByTestId("incidents-view");

    fireEvent.keyDown(screen.getByTestId("incidents-view"), { key: "c" });

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText(/what's happening/i)).toBeInTheDocument();
  });

  it("shows the blocked remediation plan on the remediation tab", async () => {
    renderView(makeClient());
    await screen.findByTestId("incident-detail");

    fireEvent.click(screen.getByRole("tab", { name: /remediation/i }));

    expect(await screen.findByTestId("remediation-plan")).toBeInTheDocument();
    expect(
      screen.getByText(/exceed the incident's blast-radius policy/i),
    ).toBeInTheDocument();
  });

  it("shows the postmortem and its action items on the postmortem tab", async () => {
    renderView(makeClient());
    await screen.findByTestId("incident-detail");

    fireEvent.click(screen.getByRole("tab", { name: /postmortem/i }));

    expect(await screen.findByTestId("postmortem")).toBeInTheDocument();
    expect(screen.getByText("ENG-201")).toBeInTheDocument();
    expect(screen.getByText(/connection pool exhaustion/i)).toBeInTheDocument();
  });

  it("shows the empty state when there are no incidents", async () => {
    renderView(makeClient({ listIncidentRecords: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("empty-queue")).toBeInTheDocument();
    expect(screen.getByText(/no open incidents/i)).toBeInTheDocument();
  });

  it("renders the queue skeleton while loading", () => {
    renderView(
      makeClient({
        listIncidentRecords: vi.fn(() => new Promise<IncidentView[]>(() => {})),
      }),
    );
    expect(screen.getByTestId("queue-skeleton")).toBeInTheDocument();
  });

  it("degrades gracefully when the incidents API errors", async () => {
    renderView(
      makeClient({
        listIncidentRecords: vi.fn(() => Promise.reject(new Error("offline"))),
      }),
    );
    expect(
      await screen.findByText(/live incidents are unavailable/i),
    ).toBeInTheDocument();
  });
});
