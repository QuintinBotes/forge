import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  CalendarRange,
  Flag,
  Gauge,
  KanbanSquare,
  LayoutList,
  Layers,
  Route,
  Rocket,
  ScrollText,
  ShieldCheck,
  Store,
  TrendingUp,
  KeyRound,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { ForgeMark } from "@/components/forge-logo";
import { cn } from "@/lib/utils";

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
}

/**
 * Primary board navigation (spec: List, Board, Roadmap, Milestones, Incidents).
 * Task 1.6 fills in the corresponding views/routes; this is the shell.
 */
export const BOARD_NAV: NavItem[] = [
  { label: "List", href: "/", icon: LayoutList },
  { label: "Board", href: "/board", icon: KanbanSquare },
  { label: "Board depth", href: "/depth", icon: Layers },
  { label: "Roadmap", href: "/roadmap", icon: CalendarRange },
  { label: "Milestones", href: "/milestones", icon: Flag },
  { label: "Sprints", href: "/sprints", icon: TrendingUp },
  { label: "Incidents", href: "/incidents", icon: AlertTriangle },
  { label: "Specs", href: "/specs", icon: Route },
  { label: "Approvals", href: "/approvals", icon: ShieldCheck },
  { label: "Deployments", href: "/deployments", icon: Rocket },
  { label: "Runs", href: "/runs", icon: Activity },
  { label: "Observability", href: "/observability", icon: Gauge },
  { label: "Audit", href: "/audit", icon: ScrollText },
  { label: "Marketplace", href: "/marketplace", icon: Store },
  { label: "SSO", href: "/settings/sso", icon: KeyRound },
];

export interface AppShellProps {
  children: ReactNode;
  /** Optional slot rendered on the right of the top bar (e.g. user menu). */
  actions?: ReactNode;
}

/**
 * The board chrome: a fixed sidebar with primary navigation, a top bar with the
 * command-palette hint, and the main content region. Purely presentational so it
 * renders without any client context (and is trivially testable).
 */
export function AppShell({ children, actions }: AppShellProps): ReactNode {
  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex h-14 items-center gap-2 px-5 font-display text-lg font-semibold tracking-tight">
          <ForgeMark className="h-7 w-7" />
          Forge
        </div>
        <nav aria-label="Primary" className="flex flex-1 flex-col gap-1 px-3 py-2">
          {BOARD_NAV.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium",
                  "text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <Icon aria-hidden className="h-4 w-4" />
                {item.label}
              </Link>
            );
          })}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-4 border-b border-border px-6">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span>Workspace</span>
          </div>
          <div className="flex items-center gap-3">
            {actions}
            <span className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground">
              Command palette
              <kbd className="font-mono text-[11px]">⌘K</kbd>
            </span>
          </div>
        </header>

        <main role="main" className="min-w-0 flex-1 overflow-auto p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
