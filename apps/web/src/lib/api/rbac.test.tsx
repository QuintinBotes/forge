import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { type ForgeApiClient } from "./client";
import {
  rbacKeys,
  useAddTeamMember,
  useCreateRoleGrant,
  useCreateTeam,
  useProjectAccess,
  useRevokeRoleGrant,
  useRoleGrants,
  useSetMemberRole,
  useSetProjectVisibility,
  useTeamMembers,
} from "./rbac";
import type { ProjectAccess, RoleGrant } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

const WS = "99999999-9999-9999-9999-999999999999";

function grant(over: Partial<RoleGrant> = {}): RoleGrant {
  return {
    id: "g1",
    workspace_id: WS,
    principal: { type: "user", id: "aaaaaaaa-0000-0000-0000-000000000000" },
    scope: { type: "workspace", id: WS },
    role: "member",
    ...over,
  };
}

describe("useRoleGrants", () => {
  it("passes the scope filter through to the client", async () => {
    const client = {
      listRoleGrants: vi.fn(() => Promise.resolve([grant()])),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => useRoleGrants({ scope_type: "workspace" }, client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.listRoleGrants).toHaveBeenCalledWith({ scope_type: "workspace" });
    expect(result.current.data).toHaveLength(1);
  });
});

describe("useCreateRoleGrant", () => {
  it("creates a grant then revalidates every grant list", async () => {
    const client = {
      createRoleGrant: vi.fn(() => Promise.resolve(grant({ role: "admin" }))),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useCreateRoleGrant(client), {
      wrapper: makeWrapper(queryClient),
    });
    result.current.mutate({
      principal: { type: "user", id: "aaaaaaaa-0000-0000-0000-000000000000" },
      scope: { type: "workspace", id: WS },
      role: "admin",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith({ queryKey: rbacKeys.grantsRoot() });
  });
});

describe("useSetMemberRole", () => {
  it("issues the new grant before revoking the old one", async () => {
    const order: string[] = [];
    const client = {
      createRoleGrant: vi.fn(() => {
        order.push("create");
        return Promise.resolve(grant({ id: "g2", role: "admin" }));
      }),
      revokeRoleGrant: vi.fn((id: string) => {
        order.push(`revoke:${id}`);
        return Promise.resolve();
      }),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSetMemberRole(client), {
      wrapper: makeWrapper(newClient()),
    });
    result.current.mutate({ grant: grant({ id: "g1" }), role: "admin" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(order).toEqual(["create", "revoke:g1"]);
    expect(client.createRoleGrant).toHaveBeenCalledWith(
      expect.objectContaining({ role: "admin", scope: { type: "workspace", id: WS } }),
    );
  });
});

describe("useRevokeRoleGrant", () => {
  it("revokes then revalidates grants", async () => {
    const client = {
      revokeRoleGrant: vi.fn(() => Promise.resolve()),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useRevokeRoleGrant(client), {
      wrapper: makeWrapper(queryClient),
    });
    result.current.mutate("g1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.revokeRoleGrant).toHaveBeenCalledWith("g1");
    expect(spy).toHaveBeenCalledWith({ queryKey: rbacKeys.grantsRoot() });
  });
});

describe("useCreateTeam", () => {
  it("invalidates the team list after creating", async () => {
    const client = {
      createTeam: vi.fn(() =>
        Promise.resolve({ id: "t1", key: "plat", name: "Platform", created_at: "x" }),
      ),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useCreateTeam(client), {
      wrapper: makeWrapper(queryClient),
    });
    result.current.mutate({ key: "plat", name: "Platform" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith({ queryKey: rbacKeys.teams() });
  });
});

describe("useTeamMembers", () => {
  it("is disabled until a team id is supplied", () => {
    const client = {
      listTeamMembers: vi.fn(() => Promise.resolve([])),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useTeamMembers("", client), {
      wrapper: makeWrapper(newClient()),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(client.listTeamMembers).not.toHaveBeenCalled();
  });
});

describe("useAddTeamMember", () => {
  it("invalidates that team's roster", async () => {
    const client = {
      addTeamMember: vi.fn(() =>
        Promise.resolve({ user_id: "u1", team_role: "member", created_at: "x" }),
      ),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useAddTeamMember(client), {
      wrapper: makeWrapper(queryClient),
    });
    result.current.mutate({ teamId: "t1", body: { user_id: "u1" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith({ queryKey: rbacKeys.teamMembers("t1") });
  });
});

describe("useProjectAccess", () => {
  it("is disabled until a project id is supplied", () => {
    const client = {
      getProjectAccess: vi.fn(),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useProjectAccess("", client), {
      wrapper: makeWrapper(newClient()),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(client.getProjectAccess).not.toHaveBeenCalled();
  });
});

describe("useSetProjectVisibility", () => {
  it("seeds the project-access cache with the returned access", async () => {
    const access: ProjectAccess = {
      project_id: "p1",
      visibility: "team_restricted",
      owner_team_id: null,
      team_access: [],
    };
    const client = {
      setProjectVisibility: vi.fn(() => Promise.resolve(access)),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();

    const { result } = renderHook(() => useSetProjectVisibility(client), {
      wrapper: makeWrapper(queryClient),
    });
    result.current.mutate({
      projectId: "p1",
      body: { visibility: "team_restricted" },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(queryClient.getQueryData(rbacKeys.projectAccess("p1"))).toEqual(access);
  });
});
