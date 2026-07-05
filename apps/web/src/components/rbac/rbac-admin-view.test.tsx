import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  ProjectAccess,
  RoleGrant,
  Team,
  TeamMember,
} from "@/lib/api/types";

import { RbacAdminView } from "./rbac-admin-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const WS = "99999999-9999-9999-9999-999999999999";
const A = "aaaaaaaa-0000-0000-0000-000000000000";
const NEW_USER = "cccccccc-0000-0000-0000-000000000000";
const T1 = "11111111-1111-1111-1111-111111111111";
const U1 = "dddddddd-0000-0000-0000-000000000000";
const P1 = "22222222-2222-2222-2222-222222222222";

const WORKSPACE_GRANTS: RoleGrant[] = [
  {
    id: "g1",
    workspace_id: WS,
    principal: { type: "user", id: A },
    scope: { type: "workspace", id: WS },
    role: "member",
  },
];

const PROJECT_GRANTS: RoleGrant[] = [
  {
    id: "gp1",
    workspace_id: WS,
    principal: { type: "user", id: A },
    scope: { type: "project", id: P1 },
    role: "member",
  },
];

const TEAMS: Team[] = [
  { id: T1, key: "plat", name: "Platform", created_at: "2026-07-01T00:00:00Z" },
];

const TEAM_MEMBERS: TeamMember[] = [
  { user_id: U1, team_role: "member", created_at: "2026-07-01T00:00:00Z" },
];

const PROJECT_ACCESS: ProjectAccess = {
  project_id: P1,
  visibility: "workspace",
  owner_team_id: null,
  team_access: [{ project_id: P1, team_id: T1, access_level: "read" }],
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    baseUrl: "http://localhost:8000",
    me: vi.fn(() => Promise.resolve({ user_id: "me", workspace_id: WS })),
    listRoleGrants: vi.fn((q?: { scope_type?: string }) =>
      Promise.resolve(q?.scope_type === "project" ? PROJECT_GRANTS : WORKSPACE_GRANTS),
    ),
    createRoleGrant: vi.fn(() => Promise.resolve(WORKSPACE_GRANTS[0])),
    revokeRoleGrant: vi.fn(() => Promise.resolve(undefined)),
    listTeams: vi.fn(() => Promise.resolve(TEAMS)),
    createTeam: vi.fn((b: { key: string; name: string }) =>
      Promise.resolve({ id: "t-new", key: b.key, name: b.name, created_at: "x" }),
    ),
    listTeamMembers: vi.fn(() => Promise.resolve(TEAM_MEMBERS)),
    addTeamMember: vi.fn(() =>
      Promise.resolve({ user_id: NEW_USER, team_role: "member", created_at: "x" }),
    ),
    setTeamMemberRole: vi.fn(() =>
      Promise.resolve({ user_id: U1, team_role: "lead", created_at: "x" }),
    ),
    removeTeamMember: vi.fn(() => Promise.resolve(undefined)),
    getProjectAccess: vi.fn(() => Promise.resolve(PROJECT_ACCESS)),
    setProjectVisibility: vi.fn((_id: string, b: { visibility: string }) =>
      Promise.resolve({ ...PROJECT_ACCESS, visibility: b.visibility }),
    ),
    upsertProjectTeamAccess: vi.fn((id: string, b: unknown) =>
      Promise.resolve({ project_id: id, ...(b as object) }),
    ),
    removeProjectTeamAccess: vi.fn(() => Promise.resolve(undefined)),
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
  return render(<RbacAdminView client={client} />, { wrapper: Wrapper });
}

const goTo = (tab: "members" | "teams" | "projects") =>
  fireEvent.click(screen.getByTestId(`rbac-tab-${tab}`));

describe("RbacAdminView — chrome", () => {
  it("renders the header and the three scope tabs", () => {
    renderView(makeClient());
    expect(screen.getByRole("heading", { name: /access control/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /members/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /^teams/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /project access/i })).toBeInTheDocument();
  });

  it("moves between tabs with the arrow keys", () => {
    renderView(makeClient());
    const tablist = screen.getByRole("tablist");
    fireEvent.keyDown(tablist, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: /^teams/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

describe("RbacAdminView — members", () => {
  it("shows a loading skeleton while grants load", () => {
    renderView(
      makeClient({ listRoleGrants: vi.fn(() => new Promise<RoleGrant[]>(() => {})) }),
    );
    expect(screen.getByTestId("members-skeleton")).toBeInTheDocument();
  });

  it("shows an error state when grants fail to load", async () => {
    renderView(
      makeClient({
        listRoleGrants: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
      }),
    );
    expect(await screen.findByTestId("members-error")).toBeInTheDocument();
  });

  it("shows the empty state with no workspace grants", async () => {
    renderView(makeClient({ listRoleGrants: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("members-empty")).toBeInTheDocument();
  });

  it("renders the role census and a member row", async () => {
    renderView(makeClient());
    expect(await screen.findByTestId("role-census")).toBeInTheDocument();
    expect(screen.getByTestId("member-row")).toBeInTheDocument();
  });

  it("adds a member with the chosen role at workspace scope", async () => {
    const client = makeClient();
    renderView(client);
    fireEvent.click(await screen.findByTestId("members-add"));
    const form = screen.getByTestId("members-add-form");
    fireEvent.change(within(form).getByLabelText(/Principal ID/i), {
      target: { value: NEW_USER },
    });
    fireEvent.change(within(form).getByLabelText("Role"), {
      target: { value: "admin" },
    });
    fireEvent.click(screen.getByTestId("members-add-submit"));

    await waitFor(() =>
      expect(client.createRoleGrant).toHaveBeenCalledWith(
        expect.objectContaining({
          principal: { type: "user", id: NEW_USER },
          scope: { type: "workspace", id: WS },
          role: "admin",
        }),
      ),
    );
  });

  it("changes a member's role (issues the new grant, revokes the old)", async () => {
    const client = makeClient();
    renderView(client);
    const select = await screen.findByLabelText(/Role for/i);
    fireEvent.change(select, { target: { value: "admin" } });

    await waitFor(() =>
      expect(client.createRoleGrant).toHaveBeenCalledWith(
        expect.objectContaining({ role: "admin" }),
      ),
    );
    await waitFor(() => expect(client.revokeRoleGrant).toHaveBeenCalledWith("g1"));
  });

  it("revokes a member after confirming", async () => {
    const client = makeClient();
    renderView(client);
    fireEvent.click(await screen.findByTestId("member-revoke"));
    fireEvent.click(await screen.findByTestId("member-revoke-confirm"));

    await waitFor(() => expect(client.revokeRoleGrant).toHaveBeenCalledWith("g1"));
  });
});

describe("RbacAdminView — teams", () => {
  it("opens the first team and lists its members", async () => {
    const client = makeClient();
    renderView(client);
    goTo("teams");

    expect(await screen.findByTestId("team-detail")).toBeInTheDocument();
    expect(await screen.findByTestId("team-member-row")).toBeInTheDocument();
    expect(client.listTeamMembers).toHaveBeenCalledWith(T1);
  });

  it("shows the teams empty state with no teams", async () => {
    renderView(makeClient({ listTeams: vi.fn(() => Promise.resolve([])) }));
    goTo("teams");
    expect(await screen.findByTestId("teams-empty")).toBeInTheDocument();
  });

  it("creates a team", async () => {
    const client = makeClient();
    renderView(client);
    goTo("teams");
    fireEvent.click(await screen.findByTestId("team-new"));
    const form = screen.getByTestId("team-create-form");
    fireEvent.change(within(form).getByLabelText(/Key/i), {
      target: { value: "design" },
    });
    fireEvent.change(within(form).getByLabelText("Name"), {
      target: { value: "Design" },
    });
    fireEvent.click(screen.getByTestId("team-create-submit"));

    await waitFor(() =>
      expect(client.createTeam).toHaveBeenCalledWith(
        expect.objectContaining({ key: "design", name: "Design" }),
      ),
    );
  });

  it("adds a member to the selected team", async () => {
    const client = makeClient();
    renderView(client);
    goTo("teams");
    fireEvent.click(await screen.findByTestId("team-member-add"));
    const form = screen.getByTestId("team-member-add-form");
    fireEvent.change(within(form).getByLabelText(/User ID/i), {
      target: { value: NEW_USER },
    });
    fireEvent.click(screen.getByTestId("team-member-add-submit"));

    await waitFor(() =>
      expect(client.addTeamMember).toHaveBeenCalledWith(T1, {
        user_id: NEW_USER,
        team_role: "member",
      }),
    );
  });
});

describe("RbacAdminView — project access", () => {
  it("prompts to open a project, then loads its access from a recent chip", async () => {
    const client = makeClient();
    renderView(client);
    goTo("projects");

    expect(await screen.findByTestId("project-none-open")).toBeInTheDocument();
    fireEvent.click(await screen.findByTestId("project-recent-chip"));

    expect(await screen.findByTestId("project-access-detail")).toBeInTheDocument();
    expect(client.getProjectAccess).toHaveBeenCalledWith(P1);
  });

  it("changes a project's visibility", async () => {
    const client = makeClient();
    renderView(client);
    goTo("projects");
    fireEvent.click(await screen.findByTestId("project-recent-chip"));
    await screen.findByTestId("project-access-detail");

    fireEvent.click(screen.getByTestId("visibility-team_restricted"));

    await waitFor(() =>
      expect(client.setProjectVisibility).toHaveBeenCalledWith(
        P1,
        expect.objectContaining({ visibility: "team_restricted" }),
      ),
    );
  });

  it("grants a team access to the project", async () => {
    const client = makeClient();
    renderView(client);
    goTo("projects");
    fireEvent.click(await screen.findByTestId("project-recent-chip"));
    await screen.findByTestId("project-access-detail");

    fireEvent.click(screen.getByTestId("project-grant"));
    const form = screen.getByTestId("project-grant-form");
    fireEvent.change(within(form).getByLabelText("Team"), {
      target: { value: T1 },
    });
    fireEvent.change(within(form).getByLabelText("Access"), {
      target: { value: "write" },
    });
    fireEvent.click(screen.getByTestId("project-grant-submit"));

    await waitFor(() =>
      expect(client.upsertProjectTeamAccess).toHaveBeenCalledWith(P1, {
        team_id: T1,
        access_level: "write",
      }),
    );
  });

  it("shows the no-access empty state when a project has no team access", async () => {
    const client = makeClient({
      getProjectAccess: vi.fn(() =>
        Promise.resolve({ ...PROJECT_ACCESS, team_access: [] }),
      ),
    });
    renderView(client);
    goTo("projects");
    fireEvent.click(await screen.findByTestId("project-recent-chip"));

    expect(await screen.findByTestId("project-access-empty")).toBeInTheDocument();
  });

  it("surfaces a not-found project error", async () => {
    const client = makeClient({
      getProjectAccess: vi.fn(() => Promise.reject(new ApiError(404, "nope", null))),
    });
    renderView(client);
    goTo("projects");
    fireEvent.click(await screen.findByTestId("project-recent-chip"));

    expect(await screen.findByTestId("project-access-error")).toHaveTextContent(
      /not found/i,
    );
  });
});
