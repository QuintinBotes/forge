import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  AgentRole,
  AoSettingsOut,
  RoleConfigListResponse,
  RoleConfigOut,
  RoutingPreviewResponse,
} from "@/lib/api/types";

import { AoSettingsView } from "./ao-settings-view";

function makeRoleConfig(over: Partial<RoleConfigOut>[] = []): RoleConfigListResponse {
  const defaults: RoleConfigOut[] = [
    { role: "planner", model_or_tier: "senior", effort: "high", source: "default" },
    { role: "coder", model_or_tier: "medior", effort: "medium", source: "default" },
    { role: "reviewer", model_or_tier: "senior", effort: "high", source: "default" },
    { role: "spec_author", model_or_tier: "medior", effort: "medium", source: "default" },
    { role: "coordinator", model_or_tier: "senior", effort: "high", source: "default" },
  ];
  const merged = defaults.map((d) => {
    const patch = over.find((o) => o.role === d.role);
    return patch ? { ...d, ...patch } : d;
  });
  return { items: merged };
}

function makeSettings(over: Partial<AoSettingsOut> = {}): AoSettingsOut {
  return {
    workspace_id: "w-test",
    auto_route: true,
    tier_model_overrides: {},
    junior_max: 6,
    medior_max: 16,
    junior_max_is_default: true,
    medior_max_is_default: true,
    ...over,
  };
}

function makePreview(over: Partial<RoutingPreviewResponse> = {}): RoutingPreviewResponse {
  return {
    tier: "medior",
    strategy: "single",
    score: 5,
    reasons: ["kind=feature (+1)", "score=5 -> tier=medior"],
    model: "claude-sonnet-5",
    provider: "anthropic",
    junior_max: 6,
    medior_max: 16,
    auto_route_enabled: true,
    ...over,
  };
}

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listAoRoleConfig: vi.fn(() => Promise.resolve(makeRoleConfig())),
    getAoSettings: vi.fn(() => Promise.resolve(makeSettings())),
    upsertAoRoleConfig: vi.fn(
      (role: AgentRole, body: { model_or_tier: string; effort: string }) =>
        Promise.resolve({
          role,
          model_or_tier: body.model_or_tier,
          effort: body.effort,
          source: "workspace",
        } as RoleConfigOut),
    ),
    deleteAoRoleConfig: vi.fn((role: AgentRole) =>
      Promise.resolve({
        role,
        model_or_tier: "medior",
        effort: "medium",
        source: "default",
      } as RoleConfigOut),
    ),
    updateAoSettings: vi.fn((body: Partial<AoSettingsOut>) =>
      Promise.resolve(makeSettings(body as Partial<AoSettingsOut>)),
    ),
    previewAoRouting: vi.fn(() => Promise.resolve(makePreview())),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  return render(<AoSettingsView client={client} />, { wrapper: Wrapper });
}

describe("AoSettingsView", () => {
  it("renders the loading skeleton while settings load", () => {
    renderView(
      makeClient({
        getAoSettings: vi.fn(() => new Promise<AoSettingsOut>(() => {})),
      }),
    );
    expect(screen.getByTestId("ao-settings-skeleton")).toBeInTheDocument();
  });

  it("shows the error state when a settings fetch fails", async () => {
    renderView(
      makeClient({
        getAoSettings: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
      }),
    );
    expect(await screen.findByTestId("ao-settings-error")).toBeInTheDocument();
  });

  it("renders every role with its effective model/tier, effort and source", async () => {
    renderView(makeClient());

    expect(await screen.findByTestId("role-row-planner")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Planner model or tier"),
    ).toHaveValue("senior");
    expect(screen.getByLabelText("Coder model or tier")).toHaveValue("medior");
    expect(screen.getByTestId("role-source-planner")).toHaveTextContent("default");
  });

  it("saves a per-role override when the model/tier is edited", async () => {
    const client = makeClient();
    renderView(client);
    const input = await screen.findByLabelText("Coder model or tier");

    fireEvent.change(input, { target: { value: "claude-opus-4-8" } });
    expect(screen.getByTestId("role-save-coder")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("role-save-coder"));

    await waitFor(() =>
      expect(client.upsertAoRoleConfig).toHaveBeenCalledWith(
        "coder",
        { model_or_tier: "claude-opus-4-8", effort: "medium" },
        undefined,
      ),
    );
  });

  it("resets a workspace-overridden role back to default", async () => {
    const client = makeClient({
      listAoRoleConfig: vi.fn(() =>
        Promise.resolve(
          makeRoleConfig([
            { role: "coder", model_or_tier: "senior", effort: "high", source: "workspace" },
          ]),
        ),
      ),
    });
    renderView(client);

    fireEvent.click(await screen.findByTestId("role-reset-coder"));

    await waitFor(() =>
      expect(client.deleteAoRoleConfig).toHaveBeenCalledWith("coder", undefined),
    );
  });

  it("does not show a reset button for a role still on the default", async () => {
    renderView(makeClient());
    await screen.findByTestId("role-row-planner");
    expect(screen.queryByTestId("role-reset-planner")).not.toBeInTheDocument();
  });

  it("toggles auto-route and marks the settings form dirty", async () => {
    renderView(makeClient());
    await screen.findByTestId("ao-settings-view");

    const toggle = screen.getByRole("switch", { name: /auto-route enabled/i });
    fireEvent.click(toggle);

    expect(screen.getByTestId("settings-dirty")).toHaveTextContent(
      /unsaved changes/i,
    );
  });

  it("edits the tier -> model map and complexity thresholds, then saves", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("ao-settings-view");

    fireEvent.change(screen.getByLabelText("Anthropic senior model"), {
      target: { value: "claude-opus-4-9" },
    });
    fireEvent.change(screen.getByLabelText("Junior max score"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByTestId("settings-save"));

    await waitFor(() =>
      expect(client.updateAoSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          tier_model_overrides: { anthropic: { senior: "claude-opus-4-9" } },
          junior_max: 8,
        }),
      ),
    );
  });

  it("runs a live routing preview and renders the resulting tier/strategy/model", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("ao-settings-view");

    fireEvent.click(screen.getByTestId("preview-run"));

    await waitFor(() => expect(client.previewAoRouting).toHaveBeenCalled());
    expect(await screen.findByTestId("preview-result")).toBeInTheDocument();
    expect(screen.getByTestId("preview-tier")).toHaveTextContent("medior");
    expect(screen.getByTestId("preview-result")).toHaveTextContent(
      "claude-sonnet-5",
    );
  });

  it("sends the current preview form signals to the routing-preview endpoint", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("ao-settings-view");

    fireEvent.change(screen.getByLabelText("Preview kind"), {
      target: { value: "incident" },
    });
    fireEvent.click(screen.getByLabelText("Touches security"));
    fireEvent.click(screen.getByTestId("preview-run"));

    await waitFor(() =>
      expect(client.previewAoRouting).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "incident", touches_security: true }),
      ),
    );
  });
});
