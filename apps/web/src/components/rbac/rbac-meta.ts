/**
 * Pure presentation helpers for the RBAC admin surface — role/label vocabulary,
 * token-only tone classes, principal-id formatting, UUID validation, and the
 * grant-list -> member roll-up that powers the members table + role census.
 *
 * Kept framework-free (no JSX, no hooks) so it is unit-testable in isolation and
 * shared across the three panels.
 */

import { ApiError } from "@/lib/api/client";
import type {
  AccessLevel,
  PrincipalRef,
  PrincipalType,
  RoleGrant,
  TeamRole,
  WorkspaceRole,
} from "@/lib/api/types";
import { WORKSPACE_ROLES } from "@/lib/api/types";

/** Human labels for the four workspace roles (note the `agent-runner` value). */
export const ROLE_LABEL: Record<WorkspaceRole, string> = {
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
  "agent-runner": "Agent runner",
};

/** One-line "what this role can do" copy, for the assign form + legend. */
export const ROLE_BLURB: Record<WorkspaceRole, string> = {
  admin: "Full control, including access and billing.",
  member: "Create and drive work across the workspace.",
  viewer: "Read-only access to boards and runs.",
  "agent-runner": "Machine identity that executes agent runs.",
};

/** Token-only tone for a role badge. Ember tint = elevated power; amber = machine. */
export const ROLE_TONE: Record<WorkspaceRole, string> = {
  admin: "border-primary/30 bg-primary/10 text-primary",
  member: "border-border bg-secondary text-secondary-foreground",
  viewer: "border-border bg-muted text-muted-foreground",
  "agent-runner": "border-warning/40 bg-warning/10 text-warning",
};

export const TEAM_ROLE_LABEL: Record<TeamRole, string> = {
  lead: "Lead",
  member: "Member",
};

export const TEAM_ROLE_TONE: Record<TeamRole, string> = {
  lead: "border-primary/30 bg-primary/10 text-primary",
  member: "border-border bg-muted text-muted-foreground",
};

export const ACCESS_LABEL: Record<AccessLevel, string> = {
  read: "Read",
  write: "Write",
  admin: "Admin",
};

/** Monochrome intensity ramp: quiet read -> steel write -> ember admin. */
export const ACCESS_TONE: Record<AccessLevel, string> = {
  read: "border-border bg-muted text-muted-foreground",
  write: "border-border bg-secondary text-secondary-foreground",
  admin: "border-primary/30 bg-primary/10 text-primary",
};

export const PRINCIPAL_LABEL: Record<PrincipalType, string> = {
  user: "User",
  api_key: "API key",
  service: "Service",
};

/** Descending-capability rank; admin (0) is the most powerful. */
export function roleRank(role: WorkspaceRole): number {
  const i = WORKSPACE_ROLES.indexOf(role);
  return i === -1 ? WORKSPACE_ROLES.length : i;
}

/** Shorten a UUID-ish id for display (keeps it scannable without wrapping). */
export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True for a canonical UUID (the id shape every grant/principal uses). */
export function isUuid(value: string): boolean {
  return UUID_RE.test(value.trim());
}

/** A workspace member: one principal, its effective role, and the backing grant. */
export interface WorkspaceMember {
  principal: PrincipalRef;
  role: WorkspaceRole;
  /** The grant carrying `role` — the handle for change-role / revoke. */
  grant: RoleGrant;
  /** Any additional grants for the same principal (kept for revoke coverage). */
  otherGrants: RoleGrant[];
}

/**
 * Roll a flat grant list up into one row per principal. When a principal holds
 * several workspace grants, the highest-capability role wins and its grant is
 * the primary handle; the rest are tracked so a full revoke can clear them too.
 * Rows are ordered by capability, then by principal id for a stable table.
 */
export function rollUpMembers(grants: RoleGrant[]): WorkspaceMember[] {
  const byPrincipal = new Map<string, RoleGrant[]>();
  for (const grant of grants) {
    const key = `${grant.principal.type}:${grant.principal.id}`;
    const list = byPrincipal.get(key);
    if (list) list.push(grant);
    else byPrincipal.set(key, [grant]);
  }

  const members: WorkspaceMember[] = [];
  for (const list of byPrincipal.values()) {
    const sorted = [...list].sort((a, b) => roleRank(a.role) - roleRank(b.role));
    const [primary, ...others] = sorted;
    members.push({
      principal: primary.principal,
      role: primary.role,
      grant: primary,
      otherGrants: others,
    });
  }

  return members.sort(
    (a, b) =>
      roleRank(a.role) - roleRank(b.role) ||
      a.principal.id.localeCompare(b.principal.id),
  );
}

/** Count members per role, in canonical role order — powers the census strip. */
export function roleCensus(
  members: WorkspaceMember[],
): { role: WorkspaceRole; count: number }[] {
  const counts = new Map<WorkspaceRole, number>();
  for (const role of WORKSPACE_ROLES) counts.set(role, 0);
  for (const m of members) counts.set(m.role, (counts.get(m.role) ?? 0) + 1);
  return WORKSPACE_ROLES.map((role) => ({ role, count: counts.get(role) ?? 0 }));
}

/**
 * Turn an RBAC mutation failure into a plain, actionable message in the
 * interface's voice. Names the two invariants the server guards — permission
 * (403) and the last-admin lockout / conflicts (409) — and stays specific.
 */
export function describeRbacError(error: unknown, action = "save that change"): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to change access here.";
    if (error.status === 404) return "That no longer exists — it may have been removed.";
    if (error.status === 409) {
      const detail =
        error.body && typeof error.body === "object"
          ? (error.body as { detail?: unknown }).detail
          : undefined;
      const kind =
        detail && typeof detail === "object" && "error" in detail
          ? String((detail as { error?: unknown }).error)
          : "";
      if (kind.includes("admin") || kind.includes("lockout"))
        return "That would leave the workspace without an admin. Grant admin to someone else first.";
      return "That change conflicts with the current access.";
    }
    if (error.status === 400 || error.status === 422)
      return "Some details look invalid. Check the fields and try again.";
  }
  return `Couldn't ${action}. Please try again.`;
}
