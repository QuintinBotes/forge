"use client";

/**
 * Multi-team & RBAC admin (F30) — the workspace's access control plane.
 *
 * The screen is organised around the three scopes of the model, which are also
 * its tabs: workspace-wide Members (who holds which role), Teams (groups and
 * their leads), and per-project access (visibility + a team's read/write/admin).
 * Each tab owns its data and spends ember on exactly one primary action, so the
 * accent stays precious. Tabs are reachable from the keyboard and the command
 * palette.
 */

import {
  FolderLock,
  ShieldCheck,
  Users,
  UsersRound,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useCurrentPrincipal } from "@/lib/api/rbac";
import { cn } from "@/lib/utils";

import { MembersPanel } from "./members-panel";
import { ProjectAccessPanel } from "./project-access-panel";
import { TeamsPanel } from "./teams-panel";

type TabId = "members" | "teams" | "projects";

const TABS: { id: TabId; label: string; icon: LucideIcon }[] = [
  { id: "members", label: "Members", icon: Users },
  { id: "teams", label: "Teams", icon: UsersRound },
  { id: "projects", label: "Project access", icon: FolderLock },
];

export interface RbacAdminViewProps {
  client?: ForgeApiClient;
  /** Initial tab (defaults to Members). */
  initialTab?: TabId;
}

export function RbacAdminView({
  client = apiClient,
  initialTab = "members",
}: RbacAdminViewProps) {
  const [active, setActive] = useState<TabId>(initialTab);
  const me = useCurrentPrincipal(client);
  const workspaceId = me.data?.workspace_id ?? "";

  // Keyboard-first: register per-tab jumps in the command palette.
  const setActiveRef = useRef(setActive);
  useEffect(() => {
    setActiveRef.current = setActive;
  }, [setActive]);
  const commands = useMemo(
    () =>
      TABS.map((t) => ({
        id: `rbac-tab-${t.id}`,
        label: `Access: ${t.label}`,
        group: "Access",
        run: () => setActiveRef.current(t.id),
      })),
    [],
  );
  useRegisterCommands("rbac-admin", commands);

  const onTabKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      const idx = TABS.findIndex((t) => t.id === active);
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        setActive(TABS[(idx + 1) % TABS.length].id);
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive(TABS[(idx - 1 + TABS.length) % TABS.length].id);
      } else if (e.key === "Home") {
        e.preventDefault();
        setActive(TABS[0].id);
      } else if (e.key === "End") {
        e.preventDefault();
        setActive(TABS[TABS.length - 1].id);
      }
    },
    [active],
  );

  return (
    <div
      data-testid="rbac-view"
      className="mx-auto flex w-full max-w-5xl flex-col gap-6"
    >
      <header className="flex items-start gap-3">
        <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-muted/60 text-primary">
          <ShieldCheck className="h-5 w-5" aria-hidden />
        </span>
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Access control
          </h1>
          <p className="text-sm text-muted-foreground">
            Manage who can do what — across the workspace, within teams, and on
            each project.
          </p>
        </div>
      </header>

      <div
        role="tablist"
        aria-label="Access scope"
        onKeyDown={onTabKeyDown}
        className="inline-flex w-full max-w-md self-start rounded-lg border border-border bg-muted/40 p-1"
      >
        {TABS.map((t) => {
          const Icon = t.icon;
          const selected = t.id === active;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`rbac-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`rbac-panel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(t.id)}
              data-testid={`rbac-tab-${t.id}`}
              className={cn(
                "inline-flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selected
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              {t.label}
            </button>
          );
        })}
      </div>

      <div
        role="tabpanel"
        id={`rbac-panel-${active}`}
        aria-labelledby={`rbac-tab-${active}`}
      >
        {active === "members" ? (
          <MembersPanel workspaceId={workspaceId} client={client} />
        ) : active === "teams" ? (
          <TeamsPanel client={client} />
        ) : (
          <ProjectAccessPanel client={client} />
        )}
      </div>
    </div>
  );
}
