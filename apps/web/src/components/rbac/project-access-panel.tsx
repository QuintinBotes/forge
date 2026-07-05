"use client";

/**
 * Project access tab — per-project controls: a project's visibility (open to the
 * whole workspace vs. walled off to named teams) and the access level each team
 * holds on it. Since there is no project directory endpoint, a project is opened
 * by id, with quick chips for projects that already carry access rules.
 */

import {
  ArrowRight,
  FolderLock,
  Globe,
  Lock,
  ScanSearch,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import {
  useProjectAccess,
  useRemoveProjectTeamAccess,
  useRoleGrants,
  useSetProjectVisibility,
  useTeams,
  useUpsertProjectTeamAccess,
} from "@/lib/api/rbac";
import {
  ACCESS_LEVELS,
  PROJECT_VISIBILITIES,
  type AccessLevel,
  type ProjectVisibility,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  ACCESS_LABEL,
  ACCESS_TONE,
  describeRbacError,
  isUuid,
  shortId,
} from "./rbac-meta";
import {
  Badge,
  EmptyState,
  ErrorNote,
  Field,
  FIELD_CLS,
  SectionCard,
  SkeletonRows,
} from "./rbac-ui";

const VIS_META: Record<
  ProjectVisibility,
  { label: string; icon: typeof Globe; blurb: string }
> = {
  workspace: {
    label: "Workspace",
    icon: Globe,
    blurb: "Everyone in the workspace can see and open this project.",
  },
  team_restricted: {
    label: "Team-restricted",
    icon: Lock,
    blurb: "Only teams granted access below can see this project.",
  },
};

export interface ProjectAccessPanelProps {
  client: ForgeApiClient;
}

export function ProjectAccessPanel({ client }: ProjectAccessPanelProps) {
  const [projectId, setProjectId] = useState("");
  const [openInput, setOpenInput] = useState("");

  const recentQuery = useRoleGrants({ scope_type: "project" }, client);
  const recentIds = useMemo(() => {
    const seen = new Set<string>();
    for (const g of recentQuery.data ?? []) seen.add(g.scope.id);
    return [...seen].slice(0, 6);
  }, [recentQuery.data]);

  const inputValid = isUuid(openInput);

  const open = (id: string) => {
    setProjectId(id);
    setOpenInput("");
  };

  return (
    <div className="flex flex-col gap-5">
      <SectionCard
        testid="project-open"
        icon={<ScanSearch className="h-5 w-5" aria-hidden />}
        title="Open a project"
        description="Manage visibility and per-team access for a specific project."
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (inputValid) open(openInput.trim());
          }}
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
        >
          <div className="flex-1">
            <Field
              label="Project ID"
              htmlFor="project-open-id"
              hint={openInput && !inputValid ? "Enter a valid UUID" : "UUID"}
            >
              <input
                id="project-open-id"
                value={openInput}
                onChange={(e) => setOpenInput(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className={cn(FIELD_CLS, "font-mono text-xs")}
                aria-invalid={Boolean(openInput) && !inputValid}
              />
            </Field>
          </div>
          <Button
            type="submit"
            size="sm"
            disabled={!inputValid}
            data-testid="project-open-submit"
          >
            <ArrowRight aria-hidden /> Open
          </Button>
        </form>

        {recentIds.length > 0 ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Projects with access rules
            </span>
            <ul className="flex flex-wrap gap-2" data-testid="project-recent">
              {recentIds.map((id) => (
                <li key={id}>
                  <button
                    type="button"
                    onClick={() => open(id)}
                    data-testid="project-recent-chip"
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      id === projectId
                        ? "border-primary/40 bg-accent text-accent-foreground"
                        : "border-border text-muted-foreground hover:bg-accent/60",
                    )}
                  >
                    <FolderLock className="h-3 w-3" aria-hidden />
                    {shortId(id)}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </SectionCard>

      {projectId ? (
        <ProjectAccessDetail
          projectId={projectId}
          client={client}
          onClose={() => setProjectId("")}
        />
      ) : (
        <SectionCard
          icon={<ShieldCheck className="h-5 w-5" aria-hidden />}
          title="Access controls"
        >
          <EmptyState
            testid="project-none-open"
            icon={<FolderLock className="h-5 w-5" aria-hidden />}
            title="Open a project to manage its access"
          >
            Enter a project ID above, or pick one that already has access rules,
            to set its visibility and grant teams read, write, or admin access.
          </EmptyState>
        </SectionCard>
      )}
    </div>
  );
}

function ProjectAccessDetail({
  projectId,
  client,
  onClose,
}: {
  projectId: string;
  client: ForgeApiClient;
  onClose: () => void;
}) {
  const accessQuery = useProjectAccess(projectId, client);
  const teamsQuery = useTeams(client);
  const setVisibility = useSetProjectVisibility(client);
  const upsert = useUpsertProjectTeamAccess(client);
  const removeAccess = useRemoveProjectTeamAccess(client);

  const [granting, setGranting] = useState(false);
  const [grantTeam, setGrantTeam] = useState("");
  const [grantLevel, setGrantLevel] = useState<AccessLevel>("read");
  const [visError, setVisError] = useState<string | null>(null);
  const [grantError, setGrantError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const access = accessQuery.data;
  const teams = teamsQuery.data ?? [];
  const teamName = (id: string) => teams.find((t) => t.id === id)?.name ?? shortId(id);

  const changeVisibility = (visibility: ProjectVisibility) => {
    if (!access || visibility === access.visibility) return;
    setVisError(null);
    setVisibility.mutate(
      { projectId, body: { visibility, owner_team_id: access.owner_team_id ?? null } },
      { onError: (e) => setVisError(describeRbacError(e, "change visibility")) },
    );
  };

  const canGrant = isUuid(grantTeam) && !upsert.isPending;

  const submitGrant = () => {
    if (!canGrant) return;
    setGrantError(null);
    upsert.mutate(
      { projectId, body: { team_id: grantTeam, access_level: grantLevel } },
      {
        onSuccess: () => {
          setGranting(false);
          setGrantTeam("");
          setGrantLevel("read");
        },
        onError: (e) => setGrantError(describeRbacError(e, "grant that access")),
      },
    );
  };

  if (accessQuery.isLoading) {
    return (
      <SectionCard
        icon={<ShieldCheck className="h-5 w-5" aria-hidden />}
        title="Access controls"
      >
        <SkeletonRows rows={3} testid="project-access-skeleton" />
      </SectionCard>
    );
  }

  if (accessQuery.isError || !access) {
    const notFound =
      accessQuery.error instanceof ApiError && accessQuery.error.status === 404;
    return (
      <SectionCard
        icon={<ShieldCheck className="h-5 w-5" aria-hidden />}
        title="Access controls"
      >
        <EmptyState
          testid="project-access-error"
          icon={<FolderLock className="h-5 w-5" aria-hidden />}
          title={notFound ? "Project not found" : "Couldn't load access"}
          action={
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => accessQuery.refetch()}>
                Retry
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose}>
                Close
              </Button>
            </div>
          }
        >
          {notFound
            ? "No project with that ID is visible to you. Check the ID and try again."
            : "The access service is unreachable. Try again in a moment."}
        </EmptyState>
      </SectionCard>
    );
  }

  const grantAction = granting ? (
    <Button variant="outline" size="sm" onClick={() => setGranting(false)}>
      Cancel
    </Button>
  ) : (
    <Button size="sm" onClick={() => setGranting(true)} data-testid="project-grant">
      <ShieldCheck aria-hidden /> Grant access
    </Button>
  );

  return (
    <SectionCard
      testid="project-access-detail"
      icon={<ShieldCheck className="h-5 w-5" aria-hidden />}
      title="Access controls"
      description={`Project ${shortId(projectId)}`}
      actions={grantAction}
    >
      {/* Visibility segmented control */}
      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Visibility
        </span>
        <div
          role="radiogroup"
          aria-label="Project visibility"
          data-testid="visibility-control"
          className="inline-flex w-full max-w-md rounded-lg border border-border bg-muted/40 p-1"
        >
          {PROJECT_VISIBILITIES.map((v) => {
            const meta = VIS_META[v];
            const Icon = meta.icon;
            const active = access.visibility === v;
            return (
              <button
                key={v}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={setVisibility.isPending}
                onClick={() => changeVisibility(v)}
                data-testid={`visibility-${v}`}
                className={cn(
                  "inline-flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60",
                  active
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="h-4 w-4" aria-hidden />
                {meta.label}
              </button>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">
          {VIS_META[access.visibility].blurb}
        </p>
        {visError ? <ErrorNote>{visError}</ErrorNote> : null}
      </div>

      {/* Grant-access form */}
      {granting ? (
        <form
          data-testid="project-grant-form"
          onSubmit={(e) => {
            e.preventDefault();
            submitGrant();
          }}
          className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4 sm:flex-row sm:items-end"
        >
          <div className="flex-1">
            <Field label="Team" htmlFor="grant-team">
              {teams.length > 0 ? (
                <select
                  id="grant-team"
                  value={grantTeam}
                  onChange={(e) => setGrantTeam(e.target.value)}
                  className={FIELD_CLS}
                >
                  <option value="">Select a team…</option>
                  {teams.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name} ({t.key})
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="grant-team"
                  value={grantTeam}
                  onChange={(e) => setGrantTeam(e.target.value)}
                  placeholder="team UUID"
                  className={cn(FIELD_CLS, "font-mono text-xs")}
                />
              )}
            </Field>
          </div>
          <div className="w-full sm:w-36">
            <Field label="Access" htmlFor="grant-level">
              <select
                id="grant-level"
                value={grantLevel}
                onChange={(e) => setGrantLevel(e.target.value as AccessLevel)}
                className={FIELD_CLS}
              >
                {ACCESS_LEVELS.map((l) => (
                  <option key={l} value={l}>
                    {ACCESS_LABEL[l]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Button
            type="submit"
            size="sm"
            disabled={!canGrant}
            data-testid="project-grant-submit"
          >
            <ShieldCheck aria-hidden />
            {upsert.isPending ? "Granting…" : "Grant"}
          </Button>
        </form>
      ) : null}

      {grantError ? <ErrorNote>{grantError}</ErrorNote> : null}
      {rowError ? <ErrorNote>{rowError}</ErrorNote> : null}

      {/* Per-team access rows */}
      {access.team_access.length === 0 ? (
        <EmptyState
          testid="project-access-empty"
          icon={<ShieldCheck className="h-5 w-5" aria-hidden />}
          title="No teams have access"
        >
          {access.visibility === "workspace"
            ? "This project is open workspace-wide. Grant a team explicit access, or restrict visibility first."
            : "This project is team-restricted but no team can reach it yet. Grant access to a team."}
        </EmptyState>
      ) : (
        <ul className="flex flex-col divide-y divide-border/60" data-testid="project-access-list">
          {access.team_access.map((a) => (
            <li
              key={a.team_id}
              data-testid="project-access-row"
              className="flex items-center gap-3 py-3"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/50 text-muted-foreground">
                <FolderLock className="h-4 w-4" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-foreground">{teamName(a.team_id)}</p>
                <p className="truncate font-mono text-[11px] text-muted-foreground" title={a.team_id}>
                  {shortId(a.team_id)}
                </p>
              </div>
              <Badge tone={ACCESS_TONE[a.access_level]} className="hidden sm:inline-flex">
                {ACCESS_LABEL[a.access_level]}
              </Badge>
              <label className="sr-only" htmlFor={`access-${a.team_id}`}>
                Access level for {teamName(a.team_id)}
              </label>
              <select
                id={`access-${a.team_id}`}
                value={a.access_level}
                onChange={(e) => {
                  setRowError(null);
                  upsert.mutate(
                    {
                      projectId,
                      body: {
                        team_id: a.team_id,
                        access_level: e.target.value as AccessLevel,
                      },
                    },
                    {
                      onError: (err) =>
                        setRowError(describeRbacError(err, "change that access")),
                    },
                  );
                }}
                disabled={upsert.isPending}
                className={cn(FIELD_CLS, "h-8 w-24 py-0 text-xs")}
              >
                {ACCESS_LEVELS.map((l) => (
                  <option key={l} value={l}>
                    {ACCESS_LABEL[l]}
                  </option>
                ))}
              </select>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Remove ${teamName(a.team_id)} access`}
                data-testid="project-access-remove"
                disabled={removeAccess.isPending}
                onClick={() => {
                  setRowError(null);
                  removeAccess.mutate(
                    { projectId, teamId: a.team_id },
                    {
                      onError: (err) =>
                        setRowError(describeRbacError(err, "remove that access")),
                    },
                  );
                }}
              >
                <Trash2 aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}
