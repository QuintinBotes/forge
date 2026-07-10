"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  AlertTriangle,
  Cable,
  Compass,
  Cpu,
  Gauge,
  KanbanSquare,
  KeyRound,
  Layers,
  LayoutList,
  Menu,
  Rocket,
  Route,
  ScrollText,
  ShieldCheck,
  Store,
  TrendingUp,
  Trophy,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { useCommandPaletteOptional } from "@/components/command-palette";
import { ForgeMark } from "@/components/forge-logo";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
}

export interface NavSection {
  /** Section heading shown above its items. */
  label: string;
  items: NavItem[];
}

/**
 * Primary navigation, grouped into a few labelled sections so the ~18 routes
 * are scannable rather than a flat wall of links:
 * - Work  — the day-to-day execution surfaces
 * - Plan  — what's coming and how it's specified
 * - Insight — health, incidents, and the audit trail
 * - Admin — configuration, access, and distribution
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Work",
    items: [
      { label: "List", href: "/", icon: LayoutList },
      { label: "Board", href: "/board", icon: KanbanSquare },
      { label: "Board depth", href: "/depth", icon: Layers },
      { label: "Runs", href: "/runs", icon: Activity },
      { label: "Approvals", href: "/approvals", icon: ShieldCheck },
    ],
  },
  {
    label: "Plan",
    items: [
      { label: "Specs", href: "/specs", icon: Route },
      { label: "Sprints", href: "/sprints", icon: TrendingUp },
      { label: "Workflows", href: "/workflow", icon: Workflow },
    ],
  },
  {
    label: "Insight",
    items: [
      { label: "Observability", href: "/observability", icon: Gauge },
      { label: "Incidents", href: "/incidents", icon: AlertTriangle },
      { label: "Leaderboard", href: "/leaderboard", icon: Trophy },
      { label: "Audit", href: "/audit", icon: ScrollText },
    ],
  },
  {
    label: "Admin",
    items: [
      { label: "Deployments", href: "/deployments", icon: Rocket },
      { label: "Marketplace", href: "/marketplace", icon: Store },
      { label: "Models & Effort", href: "/settings/models", icon: Cpu },
      { label: "Access", href: "/settings/rbac", icon: Users },
      { label: "SSO", href: "/settings/sso", icon: KeyRound },
      { label: "Integrations", href: "/settings/integrations", icon: Cable },
    ],
  },
];

/** Standalone footer links (help / onboarding), kept out of the main sections. */
export const NAV_FOOTER: NavItem[] = [
  { label: "Walkthrough", href: "/walkthrough", icon: Compass },
];

/** Flat list of every navigable route (sections + footer), for convenience. */
export const BOARD_NAV: NavItem[] = [
  ...NAV_SECTIONS.flatMap((section) => section.items),
  ...NAV_FOOTER,
];

/** True when `href` is the active route (exact for "/", prefix otherwise). */
function isActiveHref(pathname: string | null, href: string): boolean {
  if (!pathname) {
    return false;
  }
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-card",
        active
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-accent-foreground",
      )}
    >
      {/* Ember active indicator — the one place the brand accent marks "you are here". */}
      <span
        aria-hidden
        className={cn(
          "absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-primary transition-opacity",
          active ? "opacity-100" : "opacity-0",
        )}
      />
      <Icon aria-hidden className="h-4 w-4 shrink-0" />
      {item.label}
    </Link>
  );
}

/** The sidebar body — reused by the desktop rail and the mobile drawer. */
function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <>
      <div className="flex h-14 items-center gap-2 px-5 font-display text-lg font-semibold tracking-tight">
        <ForgeMark className="h-7 w-7" />
        Forge
      </div>
      <nav aria-label="Primary" className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-1 flex-col gap-5 overflow-y-auto px-3 py-3">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label} className="flex flex-col gap-1">
              <h2 className="px-3 pb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground/70">
                {section.label}
              </h2>
              {section.items.map((item) => (
                <NavLink
                  key={item.href}
                  item={item}
                  active={isActiveHref(pathname, item.href)}
                  onNavigate={onNavigate}
                />
              ))}
            </div>
          ))}
        </div>
        <div className="flex flex-col gap-1 border-t border-border px-3 py-3">
          {NAV_FOOTER.map((item) => (
            <NavLink
              key={item.href}
              item={item}
              active={isActiveHref(pathname, item.href)}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      </nav>
    </>
  );
}

/** Discoverable command-palette trigger. Falls back to a static hint if the
 * palette provider isn't mounted (e.g. isolated tests). */
function CommandPaletteHint() {
  const palette = useCommandPaletteOptional();
  const content = (
    <>
      <span className="hidden sm:inline">Search or jump to…</span>
      <span className="sm:hidden">Search</span>
      <kbd className="rounded border border-border bg-muted px-1 font-mono text-[11px] text-muted-foreground">
        ⌘K
      </kbd>
    </>
  );
  const className =
    "inline-flex items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1";
  if (!palette) {
    return <span className={className}>{content}</span>;
  }
  return (
    <button
      type="button"
      onClick={() => palette.setOpen(true)}
      aria-label="Open command palette (Command K)"
      aria-keyshortcuts="Meta+K Control+K"
      className={className}
    >
      {content}
    </button>
  );
}

export interface AppShellProps {
  children: ReactNode;
  /** Optional slot rendered on the right of the top bar (e.g. user menu). */
  actions?: ReactNode;
}

/**
 * The board chrome: a grouped sidebar (collapsing to a drawer on small
 * screens), a top bar with the discoverable command palette, and the main
 * content region. Includes a skip link and ARIA-labelled landmarks.
 */
export function AppShell({ children, actions }: AppShellProps): ReactNode {
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close the mobile drawer on Escape.
  useEffect(() => {
    if (!mobileOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMobileOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [mobileOpen]);

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <a
        href="#main-content"
        className="sr-only left-4 top-4 z-50 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:absolute focus:outline-none focus:ring-2 focus:ring-ring"
      >
        Skip to content
      </a>

      {/* Desktop rail */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card md:flex">
        <SidebarContent />
      </aside>

      {/* Mobile drawer */}
      {mobileOpen ? (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
            className="absolute inset-0 bg-background/70 backdrop-blur-sm"
          />
          <aside
            aria-label="Primary navigation"
            className="absolute inset-y-0 left-0 flex w-64 max-w-[80%] flex-col border-r border-border bg-card shadow-xl"
          >
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-4 border-b border-border px-4 sm:px-6">
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="md:hidden"
              aria-label="Open navigation"
              aria-expanded={mobileOpen}
              onClick={() => setMobileOpen(true)}
            >
              <Menu aria-hidden className="h-5 w-5" />
            </Button>
            <span className="text-sm text-muted-foreground">Workspace</span>
          </div>
          <div className="flex items-center gap-3">
            {actions}
            <CommandPaletteHint />
          </div>
        </header>

        <main id="main-content" role="main" className="min-w-0 flex-1 overflow-auto p-4 sm:p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
