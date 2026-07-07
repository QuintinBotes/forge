"use client";

/**
 * Teams tab — the workspace's teams and who belongs to each. The left rail lists
 * teams (the tab's single ember action creates one); selecting a team opens its
 * roster on the right, where members are added, promoted to lead, or removed.
 * Managing a team is a secondary, in-context action, so only "New team" is ember.
 */

import {
  Crown,
  Plus,
  Trash2,
  User,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { type ForgeApiClient } from "@/lib/api/client";
import {
  useAddTeamMember,
  useCreateTeam,
  useRemoveTeamMember,
  useSetTeamMemberRole,
  useTeamMembers,
  useTeams,
} from "@/lib/api/rbac";
import { TEAM_ROLES, type TeamRole } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  describeRbacError,
  isUuid,
  shortId,
  TEAM_ROLE_LABEL,
  TEAM_ROLE_TONE,
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

export interface TeamsPanelProps {
  client: ForgeApiClient;
}

export function TeamsPanel({ client }: TeamsPanelProps) {
  const teamsQuery = useTeams(client);
  const create = useCreateTeam(client);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  const teams = teamsQuery.data ?? [];

  // Open the first team by default so the roster isn't empty on arrival —
  // adjusted during render rather than synced through an effect.
  if (!selectedId && teams.length > 0) {
    setSelectedId(teams[0].id);
  }

  const selected = teams.find((t) => t.id === selectedId) ?? null;

  const canCreate = key.trim().length > 0 && name.trim().length > 0 && !create.isPending;

  const closeCreate = () => {
    setCreating(false);
    setCreateError(null);
    setKey("");
    setName("");
    setDescription("");
  };

  const submitCreate = () => {
    if (!canCreate) return;
    setCreateError(null);
    create.mutate(
      {
        key: key.trim(),
        name: name.trim(),
        description: description.trim() || null,
      },
      {
        onSuccess: (team) => {
          closeCreate();
          setSelectedId(team.id);
        },
        onError: (e) => setCreateError(describeRbacError(e, "create that team")),
      },
    );
  };

  const listAction = creating ? (
    <Button variant="outline" size="sm" onClick={closeCreate}>
      <X aria-hidden /> Cancel
    </Button>
  ) : (
    <Button size="sm" onClick={() => setCreating(true)} data-testid="team-new">
      <Plus aria-hidden /> New team
    </Button>
  );

  return (
    <div className="grid gap-5 lg:grid-cols-[19rem_1fr]">
      <SectionCard
        testid="teams-panel"
        icon={<Users className="h-5 w-5" aria-hidden />}
        title="Teams"
        description={teams.length === 1 ? "1 team" : `${teams.length} teams`}
        actions={listAction}
      >
        {creating ? (
          <form
            data-testid="team-create-form"
            onSubmit={(e) => {
              e.preventDefault();
              submitCreate();
            }}
            className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4"
          >
            <Field label="Key" htmlFor="team-key" hint="short, unique">
              <input
                id="team-key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="platform"
                className={cn(FIELD_CLS, "font-mono text-xs")}
              />
            </Field>
            <Field label="Name" htmlFor="team-name">
              <input
                id="team-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Platform"
                className={FIELD_CLS}
              />
            </Field>
            <Field label="Description" htmlFor="team-desc" hint="optional">
              <input
                id="team-desc"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Owns core infrastructure"
                className={FIELD_CLS}
              />
            </Field>
            {createError ? <ErrorNote>{createError}</ErrorNote> : null}
            <div className="flex justify-end">
              <Button
                type="submit"
                size="sm"
                disabled={!canCreate}
                data-testid="team-create-submit"
              >
                <Plus aria-hidden />
                {create.isPending ? "Creating…" : "Create team"}
              </Button>
            </div>
          </form>
        ) : null}

        {teamsQuery.isLoading ? (
          <SkeletonRows rows={4} testid="teams-skeleton" />
        ) : teamsQuery.isError ? (
          <EmptyState
            testid="teams-error"
            icon={<Users className="h-5 w-5" aria-hidden />}
            title="Couldn't load teams"
            action={
              <Button variant="outline" size="sm" onClick={() => teamsQuery.refetch()}>
                Retry
              </Button>
            }
          >
            The access service is unreachable. Try again in a moment.
          </EmptyState>
        ) : teams.length === 0 ? (
          <EmptyState
            testid="teams-empty"
            icon={<Plus className="h-5 w-5" aria-hidden />}
            title="No teams yet"
            action={
              !creating ? (
                <Button size="sm" onClick={() => setCreating(true)}>
                  <Plus aria-hidden /> Create the first team
                </Button>
              ) : undefined
            }
          >
            Group members into teams to scope project access and delegate
            membership to team leads.
          </EmptyState>
        ) : (
          <ul className="flex flex-col gap-1" data-testid="team-list">
            {teams.map((t) => {
              const active = t.id === selectedId;
              return (
                <li key={t.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(t.id)}
                    aria-current={active ? "true" : undefined}
                    data-testid="team-item"
                    className={cn(
                      "flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      active
                        ? "border-primary/40 bg-accent text-accent-foreground"
                        : "border-transparent hover:bg-accent/60",
                    )}
                  >
                    <span className="min-w-0 truncate font-medium text-foreground">
                      {t.name}
                    </span>
                    <span className="shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                      {t.key}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </SectionCard>

      {selected ? (
        <TeamDetail team={selected} client={client} />
      ) : (
        <SectionCard
          icon={<Users className="h-5 w-5" aria-hidden />}
          title="Team members"
        >
          <EmptyState
            testid="team-none-selected"
            icon={<Users className="h-5 w-5" aria-hidden />}
            title="Select a team"
          >
            Choose a team on the left to view and manage its members.
          </EmptyState>
        </SectionCard>
      )}
    </div>
  );
}

function TeamDetail({
  team,
  client,
}: {
  team: { id: string; name: string; key: string; description?: string | null };
  client: ForgeApiClient;
}) {
  const membersQuery = useTeamMembers(team.id, client);
  const add = useAddTeamMember(client);
  const setRole = useSetTeamMemberRole(client);
  const remove = useRemoveTeamMember(client);

  const [adding, setAdding] = useState(false);
  const [userId, setUserId] = useState("");
  const [teamRole, setTeamRole] = useState<TeamRole>("member");
  const [addError, setAddError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const members = membersQuery.data ?? [];
  const idValid = isUuid(userId);
  const canAdd = idValid && !add.isPending;

  const submitAdd = () => {
    if (!canAdd) return;
    setAddError(null);
    add.mutate(
      { teamId: team.id, body: { user_id: userId.trim(), team_role: teamRole } },
      {
        onSuccess: () => {
          setAdding(false);
          setUserId("");
          setTeamRole("member");
        },
        onError: (e) => setAddError(describeRbacError(e, "add that member")),
      },
    );
  };

  const addAction = adding ? (
    <Button variant="ghost" size="sm" onClick={() => setAdding(false)}>
      <X aria-hidden /> Cancel
    </Button>
  ) : (
    <Button
      variant="outline"
      size="sm"
      onClick={() => setAdding(true)}
      data-testid="team-member-add"
    >
      <UserPlus aria-hidden /> Add member
    </Button>
  );

  return (
    <SectionCard
      testid="team-detail"
      icon={<Users className="h-5 w-5" aria-hidden />}
      title={team.name}
      description={team.description ?? `Team ${team.key}`}
      actions={addAction}
    >
      {adding ? (
        <form
          data-testid="team-member-add-form"
          onSubmit={(e) => {
            e.preventDefault();
            submitAdd();
          }}
          className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4 sm:flex-row sm:items-end"
        >
          <div className="flex-1">
            <Field
              label="User ID"
              htmlFor="team-member-id"
              hint={userId && !idValid ? "Enter a valid UUID" : "UUID"}
            >
              <input
                id="team-member-id"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className={cn(FIELD_CLS, "font-mono text-xs")}
                aria-invalid={Boolean(userId) && !idValid}
              />
            </Field>
          </div>
          <div className="w-full sm:w-40">
            <Field label="Team role" htmlFor="team-member-role">
              <select
                id="team-member-role"
                value={teamRole}
                onChange={(e) => setTeamRole(e.target.value as TeamRole)}
                className={FIELD_CLS}
              >
                {TEAM_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {TEAM_ROLE_LABEL[r]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Button
            type="submit"
            variant="outline"
            size="sm"
            disabled={!canAdd}
            data-testid="team-member-add-submit"
          >
            <UserPlus aria-hidden />
            {add.isPending ? "Adding…" : "Add"}
          </Button>
        </form>
      ) : null}

      {addError ? <ErrorNote>{addError}</ErrorNote> : null}
      {rowError ? <ErrorNote>{rowError}</ErrorNote> : null}

      {membersQuery.isLoading ? (
        <SkeletonRows rows={3} testid="team-members-skeleton" />
      ) : membersQuery.isError ? (
        <EmptyState
          testid="team-members-error"
          icon={<Users className="h-5 w-5" aria-hidden />}
          title="Couldn't load members"
          action={
            <Button variant="outline" size="sm" onClick={() => membersQuery.refetch()}>
              Retry
            </Button>
          }
        >
          The roster is unavailable. Try again in a moment.
        </EmptyState>
      ) : members.length === 0 ? (
        <EmptyState
          testid="team-members-empty"
          icon={<UserPlus className="h-5 w-5" aria-hidden />}
          title="No members on this team"
        >
          Add a member by their user ID to give them this team&apos;s project
          access.
        </EmptyState>
      ) : (
        <ul className="flex flex-col divide-y divide-border/60" data-testid="team-member-list">
          {members.map((m) => (
            <li
              key={m.user_id}
              data-testid="team-member-row"
              className="flex items-center gap-3 py-3"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/50 text-muted-foreground">
                {m.team_role === "lead" ? (
                  <Crown className="h-4 w-4" aria-hidden />
                ) : (
                  <User className="h-4 w-4" aria-hidden />
                )}
              </span>
              <p
                className="min-w-0 flex-1 truncate font-mono text-xs text-foreground"
                title={m.user_id}
              >
                {shortId(m.user_id)}
              </p>
              <Badge tone={TEAM_ROLE_TONE[m.team_role]} className="hidden sm:inline-flex">
                {TEAM_ROLE_LABEL[m.team_role]}
              </Badge>
              <label className="sr-only" htmlFor={`team-role-${m.user_id}`}>
                Team role for {shortId(m.user_id)}
              </label>
              <select
                id={`team-role-${m.user_id}`}
                value={m.team_role}
                onChange={(e) => {
                  setRowError(null);
                  setRole.mutate(
                    {
                      teamId: team.id,
                      userId: m.user_id,
                      teamRole: e.target.value as TeamRole,
                    },
                    {
                      onError: (err) =>
                        setRowError(describeRbacError(err, "change that role")),
                    },
                  );
                }}
                disabled={setRole.isPending}
                className={cn(FIELD_CLS, "h-8 w-28 py-0 text-xs")}
              >
                {TEAM_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {TEAM_ROLE_LABEL[r]}
                  </option>
                ))}
              </select>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Remove ${shortId(m.user_id)} from ${team.name}`}
                data-testid="team-member-remove"
                disabled={remove.isPending}
                onClick={() => {
                  setRowError(null);
                  remove.mutate(
                    { teamId: team.id, userId: m.user_id },
                    {
                      onError: (err) =>
                        setRowError(describeRbacError(err, "remove that member")),
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
