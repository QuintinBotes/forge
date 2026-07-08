"use client";

import {
  Cpu,
  Gauge,
  RotateCcw,
  Route,
  Save,
  Sparkles,
  Wand2,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useAoRoleConfig,
  useAoSettings,
  useDeleteAoRoleConfig,
  usePreviewAoRouting,
  useUpdateAoSettings,
  useUpsertAoRoleConfig,
} from "@/lib/api/ao-settings";
import type {
  AgentRole,
  AoEffort,
  AoSettingsOut,
  RoleConfigOut,
  RoutingPreviewRequest,
  RoutingPreviewResponse,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const ROLES: AgentRole[] = [
  "planner",
  "coder",
  "reviewer",
  "spec_author",
  "coordinator",
];

const ROLE_LABELS: Record<AgentRole, string> = {
  planner: "Planner",
  coder: "Coder",
  reviewer: "Reviewer",
  spec_author: "Spec author",
  coordinator: "Coordinator",
};

const EFFORTS: AoEffort[] = ["low", "medium", "high", "max"];

const TIERS = ["junior", "medior", "senior"] as const;
type UiTier = (typeof TIERS)[number];

const TIER_HINTS: Record<UiTier, string> = {
  junior: "Fast / cheap — small, well-scoped work",
  medior: "Balanced — the common case",
  senior: "Frontier — large, high-risk, or underspecified work",
};

/** Providers the tier-model map editor exposes (mirrors the model router). */
const PROVIDERS = ["anthropic", "openai"] as const;
type UiProvider = (typeof PROVIDERS)[number];

const PROVIDER_LABELS: Record<UiProvider, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
};

/** Router defaults shown as placeholders only — the workspace overrides these. */
const ROUTER_DEFAULTS: Record<UiProvider, Record<UiTier, string>> = {
  anthropic: {
    junior: "claude-haiku-4-5",
    medior: "claude-sonnet-5",
    senior: "claude-opus-4-8",
  },
  openai: {
    junior: "gpt-4.1-mini",
    medior: "gpt-4.1",
    senior: "o3",
  },
};

// --- Role draft state ------------------------------------------------------- //

interface RoleDraft {
  modelOrTier: string;
  effort: AoEffort;
}

function draftFromRole(role: RoleConfigOut): RoleDraft {
  return { modelOrTier: role.model_or_tier, effort: role.effort };
}

function draftsIdentity(items: RoleConfigOut[]): string {
  return JSON.stringify(
    items.map((i) => [i.role, i.model_or_tier, i.effort, i.source]),
  );
}

// --- Workspace settings form ------------------------------------------------ //

interface SettingsForm {
  autoRoute: boolean;
  juniorMax: string;
  mediorMax: string;
  tierModels: Record<UiProvider, Record<UiTier, string>>;
}

function emptyTierModels(): Record<UiProvider, Record<UiTier, string>> {
  return {
    anthropic: { junior: "", medior: "", senior: "" },
    openai: { junior: "", medior: "", senior: "" },
  };
}

function settingsToForm(settings: AoSettingsOut | null): SettingsForm {
  const tierModels = emptyTierModels();
  if (settings) {
    for (const provider of PROVIDERS) {
      const overrides = settings.tier_model_overrides[provider];
      if (!overrides) continue;
      for (const tier of TIERS) {
        const value = overrides[tier];
        if (value) tierModels[provider][tier] = value;
      }
    }
  }
  return {
    autoRoute: settings?.auto_route ?? true,
    juniorMax: settings ? String(settings.junior_max) : "",
    mediorMax: settings ? String(settings.medior_max) : "",
    tierModels,
  };
}

function buildUpdateBody(form: SettingsForm, base: AoSettingsOut | null) {
  const tierModelOverrides: Record<string, Record<string, string>> = {};
  for (const provider of PROVIDERS) {
    const entries: Record<string, string> = {};
    for (const tier of TIERS) {
      const value = form.tierModels[provider][tier].trim();
      if (value) entries[tier] = value;
    }
    if (Object.keys(entries).length > 0) tierModelOverrides[provider] = entries;
  }

  const juniorMaxTrim = form.juniorMax.trim();
  const mediorMaxTrim = form.mediorMax.trim();

  return {
    auto_route: form.autoRoute,
    tier_model_overrides: tierModelOverrides,
    junior_max: juniorMaxTrim ? Number(juniorMaxTrim) : undefined,
    medior_max: mediorMaxTrim ? Number(mediorMaxTrim) : undefined,
    clear_junior_max: base ? !juniorMaxTrim && !base.junior_max_is_default : false,
    clear_medior_max: base ? !mediorMaxTrim && !base.medior_max_is_default : false,
  };
}

export interface AoSettingsViewProps {
  projectId?: string;
  client?: ForgeApiClient;
}

/**
 * Adaptive Orchestration "Models & Effort" settings (`ao-settings-ui`): per-role
 * model + effort selectors, the tier -> model map editor, complexity thresholds,
 * the auto-route toggle, and a live routing-preview panel. Backed by the typed
 * `/ao/role-config`, `/ao/settings` and `/ao/routing-preview` routers.
 */
export function AoSettingsView({ projectId, client = apiClient }: AoSettingsViewProps) {
  const roleQuery = useAoRoleConfig(projectId, client);
  const settingsQuery = useAoSettings(client);

  const upsertRole = useUpsertAoRoleConfig(client);
  const deleteRole = useDeleteAoRoleConfig(client);
  const updateSettings = useUpdateAoSettings(client);
  const preview = usePreviewAoRouting(client);

  const items = roleQuery.data?.items ?? [];
  const settings = settingsQuery.data ?? null;

  const [drafts, setDrafts] = useState<Partial<Record<AgentRole, RoleDraft>>>({});
  const [seededRoles, setSeededRoles] = useState<string | null>(null);
  const [roleErrors, setRoleErrors] = useState<Partial<Record<AgentRole, string>>>({});

  const rolesId = draftsIdentity(items);
  if (rolesId !== seededRoles) {
    setSeededRoles(rolesId);
    const next: Partial<Record<AgentRole, RoleDraft>> = {};
    for (const item of items) next[item.role] = draftFromRole(item);
    setDrafts(next);
  }

  const [settingsForm, setSettingsForm] = useState<SettingsForm>(() =>
    settingsToForm(null),
  );
  const [seededSettings, setSeededSettings] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  const settingsId = settings ? JSON.stringify(settings) : null;
  if (settingsId !== seededSettings) {
    setSeededSettings(settingsId);
    setSettingsForm(settingsToForm(settings));
    setSettingsError(null);
  }

  const settingsDirty =
    JSON.stringify(settingsForm) !== JSON.stringify(settingsToForm(settings));

  const patchRole = <K extends keyof RoleDraft>(
    role: AgentRole,
    key: K,
    value: RoleDraft[K],
  ) => {
    setDrafts((prev) => ({
      ...prev,
      [role]: { ...(prev[role] ?? { modelOrTier: "", effort: "medium" }), [key]: value },
    }));
  };

  const saveRole = (role: AgentRole) => {
    const draft = drafts[role];
    if (!draft || !draft.modelOrTier.trim() || upsertRole.isPending) return;
    setRoleErrors((prev) => ({ ...prev, [role]: "" }));
    upsertRole.mutate(
      {
        role,
        body: { model_or_tier: draft.modelOrTier.trim(), effort: draft.effort },
        projectId,
      },
      {
        onError: () =>
          setRoleErrors((prev) => ({
            ...prev,
            [role]: "Couldn't save this role's config. Please try again.",
          })),
      },
    );
  };

  const resetRole = (role: AgentRole) => {
    if (deleteRole.isPending) return;
    deleteRole.mutate({ role, projectId });
  };

  const saveSettings = () => {
    if (updateSettings.isPending) return;
    setSettingsError(null);
    updateSettings.mutate(buildUpdateBody(settingsForm, settings), {
      onError: () =>
        setSettingsError("Couldn't save the workspace settings. Please try again."),
    });
  };

  if (roleQuery.isLoading || settingsQuery.isLoading) {
    return <ScreenSkeleton />;
  }
  if (roleQuery.isError || settingsQuery.isError) {
    return (
      <ScreenError
        onRetry={() => {
          void roleQuery.refetch();
          void settingsQuery.refetch();
        }}
      />
    );
  }

  return (
    <div
      data-testid="ao-settings-view"
      className="mx-auto flex w-full max-w-4xl flex-col gap-6"
    >
      {/* Signature: title + auto-route master switch */}
      <header className="flex flex-wrap items-start justify-between gap-4 rounded-xl border border-border bg-card p-5">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <Cpu className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight">
              Models &amp; effort
            </h1>
            <p className="text-sm text-muted-foreground">
              How Adaptive Orchestration sizes work and routes it to a model.
            </p>
          </div>
        </div>
        <Switch
          checked={settingsForm.autoRoute}
          onCheckedChange={(next) =>
            setSettingsForm((prev) => ({ ...prev, autoRoute: next }))
          }
          label={settingsForm.autoRoute ? "Auto-route enabled" : "Auto-route disabled"}
          description={
            settingsForm.autoRoute
              ? "New work is sized and routed automatically"
              : "Roles use their pinned model/tier only"
          }
        />
      </header>

      {/* Per-role model + effort */}
      <Card
        icon={<Sparkles className="h-5 w-5" aria-hidden />}
        title="Per-role model & effort"
        description="Each agent role runs at its own model (or tier) and effort."
      >
        <div className="flex flex-col gap-3" data-testid="role-config-list">
          {ROLES.map((role) => {
            const server = items.find((i) => i.role === role);
            const draft = drafts[role] ?? { modelOrTier: "", effort: "medium" as AoEffort };
            const dirty = server
              ? server.model_or_tier !== draft.modelOrTier ||
                server.effort !== draft.effort
              : false;
            return (
              <div
                key={role}
                data-testid={`role-row-${role}`}
                className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4 sm:flex-row sm:items-end sm:gap-4"
              >
                <div className="flex min-w-[9rem] flex-col gap-1">
                  <span className="text-sm font-medium text-foreground">
                    {ROLE_LABELS[role]}
                  </span>
                  <span
                    data-testid={`role-source-${role}`}
                    className="w-fit rounded-full border border-border bg-background px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground"
                  >
                    {server?.source ?? "default"}
                  </span>
                </div>
                <label className="flex flex-1 flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">
                    Model or tier
                  </span>
                  <input
                    aria-label={`${ROLE_LABELS[role]} model or tier`}
                    value={draft.modelOrTier}
                    onChange={(e) => patchRole(role, "modelOrTier", e.target.value)}
                    placeholder="junior / medior / senior, or a concrete model id"
                    className={cn(FIELD, "font-mono text-xs")}
                  />
                </label>
                <label className="flex w-full flex-col gap-1 sm:w-36">
                  <span className="text-xs font-medium text-muted-foreground">
                    Effort
                  </span>
                  <select
                    aria-label={`${ROLE_LABELS[role]} effort`}
                    value={draft.effort}
                    onChange={(e) =>
                      patchRole(role, "effort", e.target.value as AoEffort)
                    }
                    className={FIELD}
                  >
                    {EFFORTS.map((effort) => (
                      <option key={effort} value={effort}>
                        {effort}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    data-testid={`role-save-${role}`}
                    disabled={!dirty || !draft.modelOrTier.trim() || upsertRole.isPending}
                    onClick={() => saveRole(role)}
                    className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" aria-hidden />
                    Save
                  </button>
                  {server && server.source !== "default" ? (
                    <button
                      type="button"
                      data-testid={`role-reset-${role}`}
                      disabled={deleteRole.isPending}
                      onClick={() => resetRole(role)}
                      className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
                    >
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                      Reset
                    </button>
                  ) : null}
                </div>
                {roleErrors[role] ? (
                  <p role="alert" className="text-xs text-danger">
                    {roleErrors[role]}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      </Card>

      {/* Tier -> model map + complexity thresholds */}
      <Card
        icon={<Gauge className="h-5 w-5" aria-hidden />}
        title="Tier -> model map & complexity thresholds"
        description="Per-provider model for each seniority tier, and the score bands that separate them."
      >
        <div className="flex flex-col gap-5">
          {PROVIDERS.map((provider) => (
            <div key={provider} className="flex flex-col gap-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {PROVIDER_LABELS[provider]}
              </h3>
              <div className="grid gap-3 sm:grid-cols-3">
                {TIERS.map((tier) => (
                  <label key={tier} className="flex flex-col gap-1">
                    <span className="text-xs font-medium text-foreground">
                      {tier}
                    </span>
                    <input
                      aria-label={`${PROVIDER_LABELS[provider]} ${tier} model`}
                      value={settingsForm.tierModels[provider][tier]}
                      onChange={(e) =>
                        setSettingsForm((prev) => ({
                          ...prev,
                          tierModels: {
                            ...prev.tierModels,
                            [provider]: {
                              ...prev.tierModels[provider],
                              [tier]: e.target.value,
                            },
                          },
                        }))
                      }
                      placeholder={ROUTER_DEFAULTS[provider][tier]}
                      className={cn(FIELD, "font-mono text-xs")}
                    />
                    <span className="text-[11px] text-muted-foreground">
                      {TIER_HINTS[tier]}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          ))}

          <div className="grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="flex items-baseline justify-between text-sm font-medium text-foreground">
                Junior max score
                {settings?.junior_max_is_default ? (
                  <span className="text-[11px] font-normal text-muted-foreground">
                    default
                  </span>
                ) : null}
              </span>
              <input
                aria-label="Junior max score"
                type="number"
                value={settingsForm.juniorMax}
                onChange={(e) =>
                  setSettingsForm((prev) => ({ ...prev, juniorMax: e.target.value }))
                }
                className={FIELD}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="flex items-baseline justify-between text-sm font-medium text-foreground">
                Medior max score
                {settings?.medior_max_is_default ? (
                  <span className="text-[11px] font-normal text-muted-foreground">
                    default
                  </span>
                ) : null}
              </span>
              <input
                aria-label="Medior max score"
                type="number"
                value={settingsForm.mediorMax}
                onChange={(e) =>
                  setSettingsForm((prev) => ({ ...prev, mediorMax: e.target.value }))
                }
                className={FIELD}
              />
            </label>
          </div>

          {settingsError ? (
            <p role="alert" className="text-sm text-danger">
              {settingsError}
            </p>
          ) : null}

          <div className="flex items-center justify-between gap-3">
            <span
              data-testid="settings-dirty"
              role="status"
              aria-live="polite"
              className="text-xs text-muted-foreground"
            >
              {updateSettings.isPending
                ? "Saving…"
                : settingsDirty
                  ? "Unsaved changes"
                  : "All changes saved"}
            </span>
            <button
              type="button"
              data-testid="settings-save"
              disabled={!settingsDirty || updateSettings.isPending}
              onClick={saveSettings}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              <Save className="h-4 w-4" aria-hidden />
              Save settings
            </button>
          </div>
        </div>
      </Card>

      {/* Live routing preview */}
      <RoutingPreviewPanel
        onPreview={(body) => preview.mutate(body)}
        result={preview.data ?? null}
        pending={preview.isPending}
        error={preview.isError}
      />
    </div>
  );
}

// --- Live routing preview panel -------------------------------------------- //

interface PreviewForm {
  kind: string;
  priority: string;
  blastRadius: "" | "low" | "medium" | "high";
  fileCount: string;
  repoCount: string;
  requirementCount: string;
  acceptanceCriteriaCount: string;
  touchesContracts: boolean;
  touchesSecurity: boolean;
  underspecified: boolean;
  provider: "anthropic" | "openai";
}

const DEFAULT_PREVIEW_FORM: PreviewForm = {
  kind: "feature",
  priority: "medium",
  blastRadius: "",
  fileCount: "0",
  repoCount: "1",
  requirementCount: "0",
  acceptanceCriteriaCount: "0",
  touchesContracts: false,
  touchesSecurity: false,
  underspecified: false,
  provider: "anthropic",
};

function previewFormToRequest(form: PreviewForm): RoutingPreviewRequest {
  return {
    kind: form.kind,
    priority: form.priority,
    blast_radius: form.blastRadius || null,
    file_count: Number(form.fileCount) || 0,
    repo_count: Number(form.repoCount) || 1,
    requirement_count: Number(form.requirementCount) || 0,
    acceptance_criteria_count: Number(form.acceptanceCriteriaCount) || 0,
    touches_contracts: form.touchesContracts,
    touches_security: form.touchesSecurity,
    underspecified: form.underspecified,
    provider: form.provider,
  };
}

function RoutingPreviewPanel({
  onPreview,
  result,
  pending,
  error,
}: {
  onPreview: (body: RoutingPreviewRequest) => void;
  result: RoutingPreviewResponse | null;
  pending: boolean;
  error: boolean;
}) {
  const [form, setForm] = useState<PreviewForm>(DEFAULT_PREVIEW_FORM);

  const patch = <K extends keyof PreviewForm>(key: K, value: PreviewForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const tierTone: Record<string, string> = {
    junior: "border-success/40 bg-success/10 text-success",
    medior: "border-warning/40 bg-warning/10 text-warning",
    senior: "border-danger/40 bg-danger/10 text-danger",
  };

  return (
    <Card
      icon={<Route className="h-5 w-5" aria-hidden />}
      title="Live routing preview"
      description="See what tier, strategy and model a sample task would get right now."
    >
      <div className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">Kind</span>
            <select
              aria-label="Preview kind"
              value={form.kind}
              onChange={(e) => patch("kind", e.target.value)}
              className={FIELD}
            >
              {["doc", "chore", "bug", "spike", "feature", "change_request", "incident"].map(
                (k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ),
              )}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">Priority</span>
            <select
              aria-label="Preview priority"
              value={form.priority}
              onChange={(e) => patch("priority", e.target.value)}
              className={FIELD}
            >
              {["low", "medium", "high", "urgent"].map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Blast radius
            </span>
            <select
              aria-label="Preview blast radius"
              value={form.blastRadius}
              onChange={(e) =>
                patch("blastRadius", e.target.value as PreviewForm["blastRadius"])
              }
              className={FIELD}
            >
              <option value="">none</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              File count
            </span>
            <input
              aria-label="Preview file count"
              type="number"
              min={0}
              value={form.fileCount}
              onChange={(e) => patch("fileCount", e.target.value)}
              className={FIELD}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Repo count
            </span>
            <input
              aria-label="Preview repo count"
              type="number"
              min={1}
              value={form.repoCount}
              onChange={(e) => patch("repoCount", e.target.value)}
              className={FIELD}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Requirements
            </span>
            <input
              aria-label="Preview requirement count"
              type="number"
              min={0}
              value={form.requirementCount}
              onChange={(e) => patch("requirementCount", e.target.value)}
              className={FIELD}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Acceptance criteria
            </span>
            <input
              aria-label="Preview acceptance criteria count"
              type="number"
              min={0}
              value={form.acceptanceCriteriaCount}
              onChange={(e) => patch("acceptanceCriteriaCount", e.target.value)}
              className={FIELD}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Provider
            </span>
            <select
              aria-label="Preview provider"
              value={form.provider}
              onChange={(e) =>
                patch("provider", e.target.value as PreviewForm["provider"])
              }
              className={FIELD}
            >
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
            </select>
          </label>
        </div>

        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={form.touchesContracts}
              onChange={(e) => patch("touchesContracts", e.target.checked)}
              className="h-4 w-4 rounded border-input"
            />
            Touches contracts
          </label>
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={form.touchesSecurity}
              onChange={(e) => patch("touchesSecurity", e.target.checked)}
              className="h-4 w-4 rounded border-input"
            />
            Touches security
          </label>
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={form.underspecified}
              onChange={(e) => patch("underspecified", e.target.checked)}
              className="h-4 w-4 rounded border-input"
            />
            Underspecified
          </label>
        </div>

        <button
          type="button"
          data-testid="preview-run"
          onClick={() => onPreview(previewFormToRequest(form))}
          disabled={pending}
          className="inline-flex h-9 w-fit items-center gap-2 rounded-md border border-border px-4 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          <Wand2 className="h-4 w-4" aria-hidden />
          {pending ? "Previewing…" : "Preview routing"}
        </button>

        {error ? (
          <p role="alert" className="text-sm text-danger">
            Couldn&apos;t compute a preview. Please try again.
          </p>
        ) : null}

        {result ? (
          <div
            data-testid="preview-result"
            className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span
                data-testid="preview-tier"
                className={cn(
                  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
                  tierTone[result.tier] ?? "border-border bg-muted text-muted-foreground",
                )}
              >
                {result.tier}
              </span>
              <span className="inline-flex items-center rounded-full border border-border bg-background px-2.5 py-0.5 text-xs font-medium text-foreground">
                {result.strategy}
              </span>
              <span className="font-mono text-xs text-muted-foreground">
                {result.provider} · {result.model}
              </span>
              <span className="text-xs text-muted-foreground">
                score {result.score}
              </span>
              {!result.auto_route_enabled ? (
                <span className="text-xs text-warning">
                  auto-route is off — this is informational only
                </span>
              ) : null}
            </div>
            <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
              {result.reasons.map((reason, idx) => (
                <li key={`${idx}-${reason}`}>{reason}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

// --- Shared primitives ------------------------------------------------------ //

function Switch({
  checked,
  onCheckedChange,
  label,
  description,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label: string;
  description?: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex flex-col items-end">
        <span className="text-sm font-medium text-foreground">{label}</span>
        {description ? (
          <span className="text-xs text-muted-foreground">{description}</span>
        ) : null}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onCheckedChange(!checked)}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          checked ? "bg-success" : "bg-muted-foreground/30",
        )}
      >
        <span
          className={cn(
            "inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          )}
        />
      </button>
    </div>
  );
}

function Card({
  icon,
  title,
  description,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
          {icon}
        </span>
        <div>
          <h2 className="font-display text-base font-semibold tracking-tight">
            {title}
          </h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function ScreenSkeleton() {
  return (
    <div
      data-testid="ao-settings-skeleton"
      aria-busy="true"
      className="mx-auto flex w-full max-w-4xl flex-col gap-6"
    >
      <div className="h-24 animate-pulse rounded-xl border border-border bg-card" />
      <div className="h-64 animate-pulse rounded-xl border border-border bg-card" />
      <div className="h-64 animate-pulse rounded-xl border border-border bg-card" />
    </div>
  );
}

function ScreenError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="ao-settings-error"
      role="status"
      className="mx-auto flex w-full max-w-4xl flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 py-16 text-center"
    >
      <Cpu className="h-8 w-8 text-muted-foreground" aria-hidden />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">
          Adaptive Orchestration settings unavailable
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          The settings service is unreachable. Try again in a moment.
        </p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}
