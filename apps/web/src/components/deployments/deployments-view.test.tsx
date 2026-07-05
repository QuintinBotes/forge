import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  DeploymentDetail,
  DeploymentRead,
  PipelineRead,
} from "@/lib/api/types";

import { DeploymentsView } from "./deployments-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const DEV_SHA = "aaa1111beefcafe";
const STG_SHA = "bbb2222beefcafe";
const AWAIT_SHA = "ccc3333beefcafe";

function dep(id: string, over: Partial<DeploymentRead> = {}): DeploymentRead {
  return {
    id,
    project_id: "default",
    environment_name: "staging",
    repo_id: "acme/web",
    commit_sha: STG_SHA,
    kind: "promotion",
    state: "succeeded",
    trigger: "manual",
    initiated_by: "user:alice",
    health_status: "passing",
    requested_at: "2026-07-05T10:00:00Z",
    ...over,
  };
}

const deployments: DeploymentRead[] = [
  dep("d-dev", {
    environment_name: "dev",
    commit_sha: DEV_SHA,
    state: "succeeded",
    requested_at: "2026-07-05T08:00:00Z",
  }),
  dep("d-staging", {
    environment_name: "staging",
    from_environment_name: "dev",
    commit_sha: STG_SHA,
    state: "succeeded",
    requested_at: "2026-07-05T09:00:00Z",
  }),
  dep("d-await", {
    environment_name: "staging",
    from_environment_name: "dev",
    commit_sha: AWAIT_SHA,
    state: "awaiting_approval",
    health_status: "unknown",
    requested_at: "2026-07-05T11:00:00Z",
  }),
];

const pipeline: PipelineRead = {
  id: "pl-1",
  project_id: "default",
  repo_id: "acme/web",
  enabled: true,
  version: 3,
  environments: [
    {
      id: "env-dev",
      name: "dev",
      rank: 0,
      is_restricted: false,
      requires_approval: false,
      gate_config: {},
      provider_config: {},
      health_check: {},
      currently_deployed: deployments[0],
    },
    {
      id: "env-staging",
      name: "staging",
      rank: 1,
      is_restricted: false,
      requires_approval: true,
      gate_config: {},
      provider_config: {},
      health_check: {},
      currently_deployed: deployments[1],
    },
    {
      id: "env-prod",
      name: "prod",
      rank: 2,
      is_restricted: true,
      requires_approval: true,
      gate_config: {},
      provider_config: {},
      health_check: {},
      currently_deployed: null,
    },
  ],
};

function detailFor(id: string): DeploymentDetail {
  const base = deployments.find((d) => d.id === id) ?? deployments[0];
  const awaiting = base.state === "awaiting_approval";
  return {
    ...base,
    gate: {
      deployment_id: id,
      environment: base.environment_name,
      can_proceed: awaiting,
      requires_human_approval: awaiting,
      blocking_reasons: [],
      checks: [],
    },
    checks: [
      { name: "ci_green", status: "passed", detail: "All checks green", metrics: {} },
      { name: "security_clean", status: "passed", detail: "No findings", metrics: {} },
      { name: "not_frozen", status: "passed", detail: "", metrics: {} },
    ],
    transitions: [
      {
        sequence: 1,
        from_state: "requested",
        to_state: "gate_evaluating",
        event: "request",
        actor: "user:alice",
        created_at: "2026-07-05T11:00:00Z",
      },
    ],
    diff_since: null,
  };
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getDeploymentPipeline: vi.fn(() => Promise.resolve(pipeline)),
    listProjectDeployments: vi.fn(() => Promise.resolve(deployments)),
    getDeployment: vi.fn((id: string) => Promise.resolve(detailFor(id))),
    requestDeployment: vi.fn((_p: string, body) =>
      Promise.resolve(dep("d-new", { ...body, id: "d-new" } as Partial<DeploymentRead>)),
    ),
    decideDeployment: vi.fn((id: string) =>
      Promise.resolve(dep(id, { state: "approved" })),
    ),
    cancelDeployment: vi.fn((id: string) =>
      Promise.resolve(dep(id, { state: "cancelled" })),
    ),
    rollbackDeployment: vi.fn((id: string) =>
      Promise.resolve(dep(id, { state: "rolling_back" })),
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
  return render(<DeploymentsView client={client} />, { wrapper: Wrapper });
}

function devStage(): HTMLElement {
  const stage = screen
    .getAllByTestId("pipeline-stage")
    .find((s) => s.getAttribute("data-env") === "dev");
  if (!stage) throw new Error("dev stage not found");
  return stage;
}

describe("DeploymentsView", () => {
  it("renders the pipeline stages and auto-selects the awaiting-approval deployment", async () => {
    renderView(makeClient());

    // Pipeline stages in rank order.
    const stages = await screen.findAllByTestId("pipeline-stage");
    expect(stages.map((s) => s.getAttribute("data-env"))).toEqual([
      "dev",
      "staging",
      "prod",
    ]);
    // Prod has nothing deployed.
    expect(within(stages[2]).getByTestId("stage-empty")).toBeInTheDocument();

    // Gate panel auto-focuses the awaiting-approval deployment.
    const panel = await screen.findByTestId("gate-panel");
    expect(within(panel).getByTestId("gate-verdict")).toHaveTextContent(
      /awaiting human approval/i,
    );
    // Its gate checks are listed.
    expect(within(panel).getByText("CI green")).toBeInTheDocument();
    expect(within(panel).getByText("Security")).toBeInTheDocument();
  });

  it("shows the awaiting-approval count in the header", async () => {
    renderView(makeClient());
    expect(await screen.findByTestId("awaiting-count")).toHaveTextContent(
      "1 awaiting approval",
    );
  });

  it("approves the focused deployment from the gate action bar", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("gate-panel");

    fireEvent.click(await screen.findByTestId("approve-action"));

    await waitFor(() =>
      expect(client.decideDeployment).toHaveBeenCalledWith("d-await", {
        decision: "approve",
        note: null,
      }),
    );
  });

  it("moves the selection with the j key (keyboard-first)", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("gate-panel");
    // Initially selected: d-await (top attention).
    await waitFor(() => expect(client.getDeployment).toHaveBeenCalledWith("d-await"));

    fireEvent.keyDown(screen.getByTestId("deployments-view"), { key: "j" });

    await waitFor(() =>
      expect(client.getDeployment).toHaveBeenCalledWith("d-staging"),
    );
  });

  it("opens the promote dialog with the 'p' shortcut", async () => {
    renderView(makeClient());
    await screen.findByTestId("deployments-view");

    fireEvent.keyDown(screen.getByTestId("deployments-view"), { key: "p" });

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/promote a deployment/i)).toBeInTheDocument();
  });

  it("promotes from the header dialog (requests a deployment)", async () => {
    const client = makeClient();
    renderView(client);

    fireEvent.click(await screen.findByTestId("promote-button"));
    const dialog = await screen.findByRole("dialog");

    fireEvent.change(within(dialog).getByLabelText("Commit SHA"), {
      target: { value: "deadbee1234" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /^promote$/i }));

    await waitFor(() =>
      expect(client.requestDeployment).toHaveBeenCalledWith(
        "default",
        expect.objectContaining({
          environment: "dev",
          commit_sha: "deadbee1234",
          kind: "promotion",
        }),
      ),
    );
  });

  it("prefills the promote dialog from a stage's Promote (next env + live commit)", async () => {
    renderView(makeClient());
    await screen.findAllByTestId("pipeline-stage");

    fireEvent.click(within(devStage()).getByTestId("stage-promote"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Target environment")).toHaveValue(
      "staging",
    );
    expect(within(dialog).getByLabelText("Commit SHA")).toHaveValue(DEV_SHA);
  });

  it("rolls back a succeeded deployment selected from the list", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("gate-panel");

    // Select the dev (succeeded) deployment by its short sha (scoped to the list;
    // the sha also appears on the pipeline stage).
    const list = screen.getByTestId("deployment-list");
    fireEvent.click(within(list).getByText(DEV_SHA.slice(0, 7)));
    await waitFor(() => expect(client.getDeployment).toHaveBeenCalledWith("d-dev"));

    fireEvent.click(await screen.findByTestId("rollback-action"));
    await waitFor(() =>
      expect(client.rollbackDeployment).toHaveBeenCalledWith("d-dev"),
    );
  });

  it("shows the empty state when there are no deployments", async () => {
    renderView(makeClient({ listProjectDeployments: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("deployments-empty")).toBeInTheDocument();
    expect(screen.getByText(/no deployments yet/i)).toBeInTheDocument();
  });

  it("renders the screen skeleton while the pipeline loads", () => {
    renderView(
      makeClient({
        getDeploymentPipeline: vi.fn(() => new Promise<PipelineRead>(() => {})),
      }),
    );
    expect(screen.getByTestId("deployments-skeleton")).toBeInTheDocument();
  });

  it("shows the no-pipeline guide on a 404", async () => {
    renderView(
      makeClient({
        getDeploymentPipeline: vi.fn(() =>
          Promise.reject(new ApiError(404, "no pipeline", null)),
        ),
      }),
    );
    expect(
      await screen.findByTestId("deployments-no-pipeline"),
    ).toBeInTheDocument();
  });

  it("shows the screen error when the pipeline service fails", async () => {
    renderView(
      makeClient({
        getDeploymentPipeline: vi.fn(() => Promise.reject(new Error("offline"))),
      }),
    );
    expect(await screen.findByTestId("deployments-error")).toBeInTheDocument();
  });

  it("degrades gracefully when the deployments list errors", async () => {
    renderView(
      makeClient({
        listProjectDeployments: vi.fn(() => Promise.reject(new Error("offline"))),
      }),
    );
    expect(
      await screen.findByText(/live deployments are unavailable/i),
    ).toBeInTheDocument();
  });
});
