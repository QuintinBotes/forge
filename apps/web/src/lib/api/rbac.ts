"use client";

/**
 * TanStack Query hooks for the multi-team & RBAC admin surface (F30), over the
 * `/access/grants`, `/teams` and `/projects/{id}/access` routers.
 *
 * Kept in a dedicated module (like `sso.ts`) so the access-control surface owns
 * its query keys + cache policy. The three router families map to the three
 * scopes of the model — workspace role grants (members), teams (+ membership),
 * and per-project access — and each mutation invalidates exactly the caches it
 * touches so the tables stay coherent the instant a change lands. Changing a
 * member's role is grant-atomic: it issues the new grant, then revokes the old,
 * so the server's escalation + last-admin-lockout invariants still apply.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  Principal,
  ProjectAccess,
  ProjectTeamAccess,
  ProjectTeamAccessInput,
  ProjectVisibilityInput,
  RoleGrant,
  RoleGrantInput,
  RoleGrantQuery,
  Team,
  TeamInput,
  TeamMember,
  TeamMemberInput,
  TeamRole,
  WorkspaceRole,
} from "./types";

export const rbacKeys = {
  all: () => ["rbac"] as const,
  me: () => ["rbac", "me"] as const,
  grants: (query: RoleGrantQuery = {}) => ["rbac", "grants", query] as const,
  grantsRoot: () => ["rbac", "grants"] as const,
  teams: () => ["rbac", "teams"] as const,
  teamMembers: (teamId: string) => ["rbac", "team-members", teamId] as const,
  projectAccess: (projectId: string) =>
    ["rbac", "project-access", projectId] as const,
} as const;

/**
 * The authenticated principal — its `workspace_id` is the scope id every new
 * workspace-role grant is issued against.
 */
export function useCurrentPrincipal(
  client: ForgeApiClient = apiClient,
): UseQueryResult<Principal> {
  return useQuery({
    queryKey: rbacKeys.me(),
    queryFn: () => client.me(),
    staleTime: 5 * 60 * 1000,
  });
}

// --- Members / role grants ------------------------------------------------- //

/** Role grants in the workspace (filter by scope / principal). */
export function useRoleGrants(
  query: RoleGrantQuery = {},
  client: ForgeApiClient = apiClient,
): UseQueryResult<RoleGrant[]> {
  return useQuery({
    queryKey: rbacKeys.grants(query),
    queryFn: () => client.listRoleGrants(query),
  });
}

/** Grant a role to a principal; revalidates every grant list. */
export function useCreateRoleGrant(
  client: ForgeApiClient = apiClient,
): UseMutationResult<RoleGrant, Error, RoleGrantInput> {
  const queryClient = useQueryClient();
  return useMutation<RoleGrant, Error, RoleGrantInput>({
    mutationFn: (body) => client.createRoleGrant(body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: rbacKeys.grantsRoot() });
    },
  });
}

/** Revoke a role grant; revalidates every grant list. */
export function useRevokeRoleGrant(
  client: ForgeApiClient = apiClient,
): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (grantId) => client.revokeRoleGrant(grantId),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: rbacKeys.grantsRoot() });
    },
  });
}

export interface SetMemberRoleVariables {
  grant: RoleGrant;
  role: WorkspaceRole;
}

/**
 * Change a member's role at the grant's scope. Grant-atomic: issue the new
 * grant first, then revoke the old one, so a demotion that would strip the last
 * admin is rejected by the server before the old grant is gone.
 */
export function useSetMemberRole(
  client: ForgeApiClient = apiClient,
): UseMutationResult<RoleGrant, Error, SetMemberRoleVariables> {
  const queryClient = useQueryClient();
  return useMutation<RoleGrant, Error, SetMemberRoleVariables>({
    mutationFn: async ({ grant, role }) => {
      const next = await client.createRoleGrant({
        principal: grant.principal,
        scope: grant.scope,
        role,
        expires_at: grant.expires_at ?? null,
      });
      await client.revokeRoleGrant(grant.id);
      return next;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: rbacKeys.grantsRoot() });
    },
  });
}

// --- Teams + membership ---------------------------------------------------- //

/** Every team in the workspace. */
export function useTeams(
  client: ForgeApiClient = apiClient,
): UseQueryResult<Team[]> {
  return useQuery({
    queryKey: rbacKeys.teams(),
    queryFn: () => client.listTeams(),
  });
}

/** Create a team; revalidates the team list. */
export function useCreateTeam(
  client: ForgeApiClient = apiClient,
): UseMutationResult<Team, Error, TeamInput> {
  const queryClient = useQueryClient();
  return useMutation<Team, Error, TeamInput>({
    mutationFn: (body) => client.createTeam(body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: rbacKeys.teams() });
    },
  });
}

/** A team's members (skipped until a team is selected). */
export function useTeamMembers(
  teamId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<TeamMember[]> {
  return useQuery({
    queryKey: rbacKeys.teamMembers(teamId),
    queryFn: () => client.listTeamMembers(teamId),
    enabled: Boolean(teamId),
  });
}

export interface AddTeamMemberVariables {
  teamId: string;
  body: TeamMemberInput;
}

/** Add a member to a team; revalidates that team's roster. */
export function useAddTeamMember(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TeamMember, Error, AddTeamMemberVariables> {
  const queryClient = useQueryClient();
  return useMutation<TeamMember, Error, AddTeamMemberVariables>({
    mutationFn: ({ teamId, body }) => client.addTeamMember(teamId, body),
    onSettled: (_data, _err, { teamId }) => {
      void queryClient.invalidateQueries({
        queryKey: rbacKeys.teamMembers(teamId),
      });
    },
  });
}

export interface SetTeamMemberRoleVariables {
  teamId: string;
  userId: string;
  teamRole: TeamRole;
}

/** Change a member's team role (lead / member). */
export function useSetTeamMemberRole(
  client: ForgeApiClient = apiClient,
): UseMutationResult<TeamMember, Error, SetTeamMemberRoleVariables> {
  const queryClient = useQueryClient();
  return useMutation<TeamMember, Error, SetTeamMemberRoleVariables>({
    mutationFn: ({ teamId, userId, teamRole }) =>
      client.setTeamMemberRole(teamId, userId, teamRole),
    onSettled: (_data, _err, { teamId }) => {
      void queryClient.invalidateQueries({
        queryKey: rbacKeys.teamMembers(teamId),
      });
    },
  });
}

export interface RemoveTeamMemberVariables {
  teamId: string;
  userId: string;
}

/** Remove a member from a team. */
export function useRemoveTeamMember(
  client: ForgeApiClient = apiClient,
): UseMutationResult<void, Error, RemoveTeamMemberVariables> {
  const queryClient = useQueryClient();
  return useMutation<void, Error, RemoveTeamMemberVariables>({
    mutationFn: ({ teamId, userId }) => client.removeTeamMember(teamId, userId),
    onSettled: (_data, _err, { teamId }) => {
      void queryClient.invalidateQueries({
        queryKey: rbacKeys.teamMembers(teamId),
      });
    },
  });
}

// --- Per-project access ---------------------------------------------------- //

/** A project's visibility + per-team access (skipped until one is opened). */
export function useProjectAccess(
  projectId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ProjectAccess> {
  return useQuery({
    queryKey: rbacKeys.projectAccess(projectId),
    queryFn: () => client.getProjectAccess(projectId),
    enabled: Boolean(projectId),
  });
}

export interface SetProjectVisibilityVariables {
  projectId: string;
  body: ProjectVisibilityInput;
}

/** Set a project's visibility; seeds + revalidates its access cache. */
export function useSetProjectVisibility(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ProjectAccess, Error, SetProjectVisibilityVariables> {
  const queryClient = useQueryClient();
  return useMutation<ProjectAccess, Error, SetProjectVisibilityVariables>({
    mutationFn: ({ projectId, body }) =>
      client.setProjectVisibility(projectId, body),
    onSuccess: (access, { projectId }) => {
      queryClient.setQueryData(rbacKeys.projectAccess(projectId), access);
    },
    onSettled: (_data, _err, { projectId }) => {
      void queryClient.invalidateQueries({
        queryKey: rbacKeys.projectAccess(projectId),
      });
    },
  });
}

export interface UpsertProjectTeamAccessVariables {
  projectId: string;
  body: ProjectTeamAccessInput;
}

/** Grant or update a team's access on a project; revalidates its access. */
export function useUpsertProjectTeamAccess(
  client: ForgeApiClient = apiClient,
): UseMutationResult<
  ProjectTeamAccess,
  Error,
  UpsertProjectTeamAccessVariables
> {
  const queryClient = useQueryClient();
  return useMutation<ProjectTeamAccess, Error, UpsertProjectTeamAccessVariables>(
    {
      mutationFn: ({ projectId, body }) =>
        client.upsertProjectTeamAccess(projectId, body),
      onSettled: (_data, _err, { projectId }) => {
        void queryClient.invalidateQueries({
          queryKey: rbacKeys.projectAccess(projectId),
        });
      },
    },
  );
}

export interface RemoveProjectTeamAccessVariables {
  projectId: string;
  teamId: string;
}

/** Remove a team's access on a project; revalidates its access. */
export function useRemoveProjectTeamAccess(
  client: ForgeApiClient = apiClient,
): UseMutationResult<void, Error, RemoveProjectTeamAccessVariables> {
  const queryClient = useQueryClient();
  return useMutation<void, Error, RemoveProjectTeamAccessVariables>({
    mutationFn: ({ projectId, teamId }) =>
      client.removeProjectTeamAccess(projectId, teamId),
    onSettled: (_data, _err, { projectId }) => {
      void queryClient.invalidateQueries({
        queryKey: rbacKeys.projectAccess(projectId),
      });
    },
  });
}
