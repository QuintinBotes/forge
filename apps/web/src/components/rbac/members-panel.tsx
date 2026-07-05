"use client";

/**
 * Members tab — the workspace's people (and machine identities) and the role
 * each one holds. The role census meter at the top answers the first RBAC
 * question ("who holds what power"); the table below assigns, changes and
 * revokes roles. Ember is spent only on the single "Add member" action.
 */

import {
  KeyRound,
  Server,
  ShieldCheck,
  Trash2,
  User,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { type ForgeApiClient } from "@/lib/api/client";
import {
  useCreateRoleGrant,
  useRevokeRoleGrant,
  useRoleGrants,
  useSetMemberRole,
} from "@/lib/api/rbac";
import {
  PRINCIPAL_TYPES,
  WORKSPACE_ROLES,
  type PrincipalType,
  type WorkspaceRole,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  describeRbacError,
  isUuid,
  PRINCIPAL_LABEL,
  ROLE_BLURB,
  ROLE_LABEL,
  ROLE_TONE,
  roleCensus,
  rollUpMembers,
  shortId,
  type WorkspaceMember,
} from "./rbac-meta";
import {
  Dot,
  EmptyState,
  ErrorNote,
  Field,
  FIELD_CLS,
  SectionCard,
  SkeletonRows,
} from "./rbac-ui";

/** Distinct, token-wired fills for the census meter segments. */
const CENSUS_FILL: Record<WorkspaceRole, string> = {
  admin: "bg-primary",
  member: "bg-success",
  viewer: "bg-muted-foreground",
  "agent-runner": "bg-warning",
};

const PRINCIPAL_ICON: Record<PrincipalType, typeof User> = {
  user: User,
  api_key: KeyRound,
  service: Server,
};

export interface MembersPanelProps {
  /** Workspace UUID — the scope id new workspace grants are issued against. */
  workspaceId: string;
  client: ForgeApiClient;
}

export function MembersPanel({ workspaceId, client }: MembersPanelProps) {
  const grantsQuery = useRoleGrants({ scope_type: "workspace" }, client);
  const create = useCreateRoleGrant(client);
  const setRole = useSetMemberRole(client);
  const revoke = useRevokeRoleGrant(client);

  const [adding, setAdding] = useState(false);
  const [formType, setFormType] = useState<PrincipalType>("user");
  const [formId, setFormId] = useState("");
  const [formRole, setFormRole] = useState<WorkspaceRole>("member");
  const [formError, setFormError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const grants = useMemo(() => grantsQuery.data ?? [], [grantsQuery.data]);
  const members = useMemo(() => rollUpMembers(grants), [grants]);
  const census = useMemo(() => roleCensus(members), [members]);
  const total = members.length;
  const resolvedWorkspaceId =
    workspaceId || grants[0]?.scope.id || grants[0]?.workspace_id || "";

  const idValid = isUuid(formId);
  const canSubmit = idValid && Boolean(resolvedWorkspaceId) && !create.isPending;

  const closeForm = () => {
    setAdding(false);
    setFormError(null);
    setFormId("");
    setFormType("user");
    setFormRole("member");
  };

  const submit = () => {
    if (!canSubmit) return;
    setFormError(null);
    create.mutate(
      {
        principal: { type: formType, id: formId.trim() },
        scope: { type: "workspace", id: resolvedWorkspaceId },
        role: formRole,
      },
      {
        onSuccess: closeForm,
        onError: (e) => setFormError(describeRbacError(e, "add that member")),
      },
    );
  };

  const changeRole = (member: WorkspaceMember, role: WorkspaceRole) => {
    if (role === member.role) return;
    setRowError(null);
    setRole.mutate(
      { grant: member.grant, role },
      { onError: (e) => setRowError(describeRbacError(e, "change that role")) },
    );
  };

  const doRevoke = (member: WorkspaceMember) => {
    setRowError(null);
    revoke.mutate(member.grant.id, {
      onSuccess: () => setConfirming(null),
      onError: (e) => {
        setRowError(describeRbacError(e, "revoke that member"));
        setConfirming(null);
      },
    });
  };

  const addAction = adding ? (
    <Button variant="outline" size="sm" onClick={closeForm}>
      <X aria-hidden /> Cancel
    </Button>
  ) : (
    <Button size="sm" onClick={() => setAdding(true)} data-testid="members-add">
      <UserPlus aria-hidden /> Add member
    </Button>
  );

  if (grantsQuery.isLoading) {
    return (
      <SectionCard
        icon={<Users className="h-5 w-5" aria-hidden />}
        title="Members"
        description="People and machine identities in this workspace."
      >
        <SkeletonRows rows={4} testid="members-skeleton" />
      </SectionCard>
    );
  }

  if (grantsQuery.isError) {
    return (
      <SectionCard
        icon={<Users className="h-5 w-5" aria-hidden />}
        title="Members"
      >
        <EmptyState
          testid="members-error"
          icon={<Users className="h-5 w-5" aria-hidden />}
          title="Couldn't load members"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => grantsQuery.refetch()}
            >
              Retry
            </Button>
          }
        >
          The access service is unreachable. Your grants are safe — try again in
          a moment.
        </EmptyState>
      </SectionCard>
    );
  }

  return (
    <SectionCard
      testid="members-panel"
      icon={<Users className="h-5 w-5" aria-hidden />}
      title="Members"
      description={
        total === 1 ? "1 member with a workspace role." : `${total} members with a workspace role.`
      }
      actions={addAction}
    >
      {/* Signature: the role census — distribution of power at a glance. */}
      <RoleCensus census={census} total={total} />

      {adding ? (
        <form
          data-testid="members-add-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="flex flex-col gap-4 rounded-lg border border-border bg-muted/30 p-4"
        >
          <div className="grid gap-4 sm:grid-cols-[10rem_1fr_12rem]">
            <Field label="Identity" htmlFor="member-type">
              <select
                id="member-type"
                value={formType}
                onChange={(e) => setFormType(e.target.value as PrincipalType)}
                className={FIELD_CLS}
              >
                {PRINCIPAL_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {PRINCIPAL_LABEL[t]}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Principal ID"
              htmlFor="member-id"
              hint={formId && !idValid ? "Enter a valid UUID" : "UUID"}
            >
              <input
                id="member-id"
                value={formId}
                onChange={(e) => setFormId(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className={cn(FIELD_CLS, "font-mono text-xs")}
                aria-invalid={Boolean(formId) && !idValid}
              />
            </Field>
            <Field label="Role" htmlFor="member-role">
              <select
                id="member-role"
                value={formRole}
                onChange={(e) => setFormRole(e.target.value as WorkspaceRole)}
                className={FIELD_CLS}
              >
                {WORKSPACE_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABEL[r]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <p className="text-xs text-muted-foreground">{ROLE_BLURB[formRole]}</p>
          {formError ? <ErrorNote>{formError}</ErrorNote> : null}
          <div className="flex justify-end">
            <Button
              type="submit"
              size="sm"
              disabled={!canSubmit}
              data-testid="members-add-submit"
            >
              <UserPlus aria-hidden />
              {create.isPending ? "Adding…" : "Add member"}
            </Button>
          </div>
        </form>
      ) : null}

      {rowError ? <ErrorNote>{rowError}</ErrorNote> : null}

      {total === 0 ? (
        <EmptyState
          testid="members-empty"
          icon={<UserPlus className="h-5 w-5" aria-hidden />}
          title="No members yet"
          action={
            !adding ? (
              <Button size="sm" onClick={() => setAdding(true)}>
                <UserPlus aria-hidden /> Add the first member
              </Button>
            ) : undefined
          }
        >
          Grant someone a workspace role to get started. Roles range from
          full-control admin to read-only viewer.
        </EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 pr-4 font-medium">Principal</th>
                <th className="py-2 pr-4 font-medium">Role</th>
                <th className="py-2 pr-4 font-medium">Expires</th>
                <th className="py-2 pl-4 text-right font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => {
                const Icon = PRINCIPAL_ICON[m.principal.type];
                const isConfirming = confirming === m.grant.id;
                return (
                  <tr
                    key={`${m.principal.type}:${m.principal.id}`}
                    data-testid="member-row"
                    className="border-b border-border/60 last:border-0"
                  >
                    <td className="py-3 pr-4">
                      <div className="flex items-center gap-3">
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/50 text-muted-foreground">
                          <Icon className="h-4 w-4" aria-hidden />
                        </span>
                        <div className="min-w-0">
                          <p
                            className="truncate font-mono text-xs text-foreground"
                            title={m.principal.id}
                          >
                            {shortId(m.principal.id)}
                          </p>
                          <p className="text-[11px] text-muted-foreground">
                            {PRINCIPAL_LABEL[m.principal.type]}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td className="py-3 pr-4">
                      <label
                        className="sr-only"
                        htmlFor={`role-${m.grant.id}`}
                      >
                        Role for {shortId(m.principal.id)}
                      </label>
                      <select
                        id={`role-${m.grant.id}`}
                        value={m.role}
                        onChange={(e) =>
                          changeRole(m, e.target.value as WorkspaceRole)
                        }
                        disabled={setRole.isPending}
                        className={cn(
                          "h-8 rounded-full border px-2.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60",
                          ROLE_TONE[m.role],
                        )}
                      >
                        {WORKSPACE_ROLES.map((r) => (
                          <option key={r} value={r}>
                            {ROLE_LABEL[r]}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-3 pr-4 text-xs text-muted-foreground">
                      {m.grant.expires_at
                        ? new Date(m.grant.expires_at).toLocaleDateString()
                        : "Never"}
                    </td>
                    <td className="py-3 pl-4">
                      <div className="flex items-center justify-end gap-2">
                        {isConfirming ? (
                          <>
                            <Button
                              variant="destructive"
                              size="sm"
                              onClick={() => doRevoke(m)}
                              disabled={revoke.isPending}
                              data-testid="member-revoke-confirm"
                            >
                              {revoke.isPending ? "Revoking…" : "Confirm"}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => setConfirming(null)}
                            >
                              Cancel
                            </Button>
                          </>
                        ) : (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setConfirming(m.grant.id)}
                            aria-label={`Revoke ${shortId(m.principal.id)}`}
                            data-testid="member-revoke"
                          >
                            <Trash2 aria-hidden /> Revoke
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </SectionCard>
  );
}

function RoleCensus({
  census,
  total,
}: {
  census: { role: WorkspaceRole; count: number }[];
  total: number;
}) {
  return (
    <div className="flex flex-col gap-2.5" data-testid="role-census">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        {total === 0
          ? null
          : census
              .filter((c) => c.count > 0)
              .map((c) => (
                <span
                  key={c.role}
                  className={cn("h-full", CENSUS_FILL[c.role])}
                  style={{ width: `${(c.count / total) * 100}%` }}
                  aria-hidden
                />
              ))}
      </div>
      <ul className="flex flex-wrap gap-x-5 gap-y-1.5">
        {census.map((c) => (
          <li
            key={c.role}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            <Dot className={CENSUS_FILL[c.role]} />
            <span className="font-medium text-foreground">{c.count}</span>
            {ROLE_LABEL[c.role]}
          </li>
        ))}
      </ul>
    </div>
  );
}
