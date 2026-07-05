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
  WorkflowCatalog,
  WorkflowDefinitionDetail,
  WorkflowDefinitionSummary,
  WorkflowGraph,
  WorkflowRevisionDetail,
  WorkflowStateNode,
  WorkflowTransitionEdge,
  WorkflowValidationIssue,
} from "@/lib/api/types";

import { WorkflowEditor } from "./workflow-editor";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

// --- fixtures ------------------------------------------------------------- //

function node(
  id: string,
  kind: WorkflowStateNode["kind"],
  x: number,
): WorkflowStateNode {
  return { id, kind, layout: { x, y: 0 } };
}

function edge(over: Partial<WorkflowTransitionEdge>): WorkflowTransitionEdge {
  return {
    id: "e0",
    from_state: "created",
    to_state: "building",
    preconditions: [],
    checks: [],
    ...over,
  };
}

const customGraph: WorkflowGraph = {
  name: "release_review",
  version: "1",
  title: "Release review",
  modes: { default: "single_agent" },
  retry_policy: { max_retries: 3, backoff: "exponential", initial_delay_seconds: 30 },
  escalation_policy: {
    confidence_threshold: 0.6,
    on_low_confidence: "pause_and_notify",
    on_policy_conflict: "escalate_to_admin",
  },
  nodes: [
    node("created", "initial", 0),
    node("building", "normal", 240),
    node("closed", "terminal", 480),
  ],
  edges: [
    edge({ id: "e0", from_state: "created", to_state: "building", action: "start_agent_run" }),
    edge({ id: "e1", from_state: "building", to_state: "closed", when: "all_checks_passed" }),
  ],
};

const bundledGraph: WorkflowGraph = {
  ...customGraph,
  name: "default_feature",
  title: "Default Feature",
  nodes: [node("created", "initial", 0), node("closed", "terminal", 240)],
  edges: [edge({ id: "e0", from_state: "created", to_state: "closed", action: "close_task" })],
};

const nodeError: WorkflowValidationIssue = {
  code: "dead_end_state",
  severity: "error",
  message: "non-terminal state 'building' has no outgoing transition",
  node_id: "building",
};

function revision(over: Partial<WorkflowRevisionDetail>): WorkflowRevisionDetail {
  return {
    id: "rev-1",
    revision: 1,
    status: "draft",
    validation_status: "valid",
    error_count: 0,
    warning_count: 0,
    graph: customGraph,
    dsl_yaml: "workflow: release_review\n",
    validation_issues: [],
    ...over,
  };
}

const summaries: WorkflowDefinitionSummary[] = [
  {
    name: "default_feature",
    title: "Default Feature",
    origin: "bundled",
    is_active: true,
    published_revision: 0,
    has_draft: false,
  },
  {
    name: "release_review",
    title: "Release review",
    origin: "custom",
    is_active: true,
    published_revision: null,
    has_draft: true,
  },
];

const catalog: WorkflowCatalog = {
  states: [
    "created",
    "building",
    "closed",
    "spec_review",
    "plan_review",
    "failed",
    "cancelled",
  ],
  events: ["all_checks_passed", "ci_status_green", "review_approved_by_human"],
  guards: [
    { name: "checks_failed", description: "", takes_arg: false, is_precondition: false },
    { name: "retry_remaining", description: "", takes_arg: false, is_precondition: false },
  ],
  preconditions: [
    { name: "repo_target_set", description: "", takes_arg: false, is_precondition: true },
    { name: "policy_loaded", description: "", takes_arg: false, is_precondition: true },
  ],
  effects: [
    { name: "close_task", description: "" },
    { name: "run_checks", description: "" },
    { name: "start_agent_run", description: "" },
  ],
  skills: ["python-pro", "reviewer"],
  modes: ["single_agent", "supervised_multi_agent"],
};

function detailFor(name: string): WorkflowDefinitionDetail {
  if (name === "default_feature") {
    return {
      name: "default_feature",
      title: "Default Feature",
      origin: "bundled",
      is_active: true,
      published_revision: 0,
      has_draft: false,
      editable: false,
      current_published: revision({
        id: "rev-bundled",
        revision: 0,
        status: "published",
        validation_status: "valid",
        graph: bundledGraph,
      }),
      draft: null,
    };
  }
  return {
    name: "release_review",
    title: "Release review",
    origin: "custom",
    is_active: true,
    published_revision: null,
    has_draft: true,
    editable: true,
    current_published: null,
    draft: revision({
      id: "rev-draft",
      revision: 2,
      status: "draft",
      validation_status: "invalid",
      error_count: 1,
      graph: customGraph,
      validation_issues: [nodeError],
    }),
  };
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getWorkflowCatalog: vi.fn(() => Promise.resolve(catalog)),
    listWorkflowDefinitions: vi.fn(() => Promise.resolve(summaries)),
    getWorkflowDefinition: vi.fn((name: string) =>
      Promise.resolve(detailFor(name)),
    ),
    saveWorkflowDraft: vi.fn((_name: string, body: { graph: WorkflowGraph }) =>
      Promise.resolve(
        revision({
          id: "rev-draft",
          revision: 3,
          validation_status: "valid",
          graph: body.graph,
          validation_issues: [],
        }),
      ),
    ),
    validateWorkflowDraft: vi.fn(() =>
      Promise.resolve([] as WorkflowValidationIssue[]),
    ),
    publishWorkflow: vi.fn(() =>
      Promise.resolve(
        revision({
          id: "rev-pub",
          revision: 3,
          status: "published",
          validation_status: "valid",
        }),
      ),
    ),
    forkBundledWorkflow: vi.fn((name: string) =>
      Promise.resolve({ ...detailFor("release_review"), name }),
    ),
    createWorkflowDefinition: vi.fn((body: { name: string; title: string }) =>
      Promise.resolve({ ...detailFor("release_review"), ...body }),
    ),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderEditor(client: ForgeApiClient) {
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
  return render(<WorkflowEditor client={client} />, { wrapper: Wrapper });
}

describe("WorkflowEditor", () => {
  it("opens the in-progress draft and renders its state graph", async () => {
    renderEditor(makeClient());

    // Header names the selected definition.
    expect(await screen.findByText("Release review")).toBeInTheDocument();

    // Canvas renders each state as a focusable node.
    const canvas = await screen.findByTestId("workflow-canvas");
    expect(within(canvas).getByTestId("wf-node-created")).toBeInTheDocument();
    expect(within(canvas).getByTestId("wf-node-building")).toBeInTheDocument();
    expect(within(canvas).getByTestId("wf-node-closed")).toBeInTheDocument();

    // Transitions are drawn with their trigger label.
    expect(within(canvas).getByText("start_agent_run")).toBeInTheDocument();
  });

  it("surfaces server validation with an error count and issue list", async () => {
    renderEditor(makeClient());

    // Wait for the seeded draft issues to load, then assert the rolled-up count.
    expect(
      await screen.findByText(/no outgoing transition/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId("error-count")).toHaveTextContent("1 errors");
    // A seeded error blocks Publish.
    expect(await screen.findByTestId("publish-button")).toBeDisabled();
  });

  it("inspects a state when its node is selected", async () => {
    renderEditor(makeClient());
    const canvas = await screen.findByTestId("workflow-canvas");

    fireEvent.click(within(canvas).getByTestId("wf-node-building"));

    const inspector = await screen.findByTestId("node-inspector");
    expect(within(inspector).getByText("building")).toBeInTheDocument();
    // The state's error is echoed in the inspector.
    expect(
      within(inspector).getByText(/no outgoing transition/i),
    ).toBeInTheDocument();
  });

  it("edits a transition and saves the draft with the new graph", async () => {
    const client = makeClient();
    renderEditor(client);
    const canvas = await screen.findByTestId("workflow-canvas");

    fireEvent.click(within(canvas).getByTestId("wf-edge-e0"));
    const inspector = await screen.findByTestId("edge-inspector");

    // Save is disabled until there are unsaved changes.
    expect(screen.getByTestId("save-button")).toBeDisabled();

    fireEvent.change(within(inspector).getByLabelText("Transition effect"), {
      target: { value: "run_checks" },
    });

    expect(await screen.findByTestId("dirty-indicator")).toBeInTheDocument();
    const save = screen.getByTestId("save-button");
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() =>
      expect(client.saveWorkflowDraft).toHaveBeenCalledWith(
        "release_review",
        expect.objectContaining({
          graph: expect.objectContaining({
            edges: expect.arrayContaining([
              expect.objectContaining({ id: "e0", action: "run_checks" }),
            ]),
          }),
        }),
      ),
    );
  });

  it("validates the saved draft, then publishes once it is clean", async () => {
    const client = makeClient();
    renderEditor(client);
    await screen.findByTestId("workflow-canvas");

    fireEvent.click(await screen.findByTestId("validate-button"));

    await waitFor(() =>
      expect(client.validateWorkflowDraft).toHaveBeenCalledWith("release_review"),
    );

    // Cleared issues unblock Publish.
    const publish = await screen.findByTestId("publish-button");
    await waitFor(() => expect(publish).toBeEnabled());

    fireEvent.click(publish);
    await waitFor(() =>
      expect(client.publishWorkflow).toHaveBeenCalledWith("release_review"),
    );
  });

  it("adds a new state from the registry palette", async () => {
    renderEditor(makeClient());
    await screen.findByTestId("workflow-canvas");

    fireEvent.change(screen.getByTestId("add-state"), {
      target: { value: "spec_review" },
    });

    expect(await screen.findByTestId("wf-node-spec_review")).toBeInTheDocument();
    expect(screen.getByTestId("dirty-indicator")).toBeInTheDocument();
  });

  it("adds a transition and focuses it for editing", async () => {
    renderEditor(makeClient());
    await screen.findByTestId("workflow-canvas");

    fireEvent.click(screen.getByTestId("add-transition"));

    // The new edge opens in the inspector.
    expect(await screen.findByTestId("edge-inspector")).toBeInTheDocument();
    expect(screen.getByTestId("dirty-indicator")).toBeInTheDocument();
  });

  it("treats a bundled workflow as read-only and offers a fork", async () => {
    const client = makeClient();
    renderEditor(client);
    await screen.findByTestId("workflow-canvas");

    fireEvent.click(await screen.findByTestId("definition-default_feature"));

    const fork = await screen.findByTestId("fork-button");
    expect(screen.queryByTestId("publish-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("add-transition")).toBeDisabled();

    fireEvent.click(fork);
    await waitFor(() =>
      expect(client.forkBundledWorkflow).toHaveBeenCalledWith("default_feature"),
    );
  });

  it("clears the selection with Escape", async () => {
    renderEditor(makeClient());
    const canvas = await screen.findByTestId("workflow-canvas");

    fireEvent.click(within(canvas).getByTestId("wf-node-building"));
    await screen.findByTestId("node-inspector");

    fireEvent.keyDown(screen.getByTestId("workflow-editor"), { key: "Escape" });
    expect(await screen.findByTestId("inspector-empty")).toBeInTheDocument();
  });

  it("creates a new workflow from the dialog", async () => {
    const client = makeClient();
    renderEditor(client);
    await screen.findByTestId("workflow-canvas");

    fireEvent.click(screen.getByTestId("new-workflow"));
    const dialog = await screen.findByRole("dialog");

    fireEvent.change(within(dialog).getByLabelText("Name"), {
      target: { value: "release_gate" },
    });
    fireEvent.change(within(dialog).getByLabelText("Title"), {
      target: { value: "Release gate" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /create workflow/i }),
    );

    await waitFor(() =>
      expect(client.createWorkflowDefinition).toHaveBeenCalledWith({
        name: "release_gate",
        title: "Release gate",
      }),
    );
  });

  it("renders the skeleton while definitions load", () => {
    renderEditor(
      makeClient({
        listWorkflowDefinitions: vi.fn(
          () => new Promise<WorkflowDefinitionSummary[]>(() => {}),
        ),
      }),
    );
    expect(screen.getByTestId("workflow-skeleton")).toBeInTheDocument();
  });

  it("shows the error state when the workflow service fails", async () => {
    renderEditor(
      makeClient({
        listWorkflowDefinitions: vi.fn(() =>
          Promise.reject(new ApiError(503, "offline", null)),
        ),
      }),
    );
    expect(await screen.findByTestId("workflow-error")).toBeInTheDocument();
  });
});
