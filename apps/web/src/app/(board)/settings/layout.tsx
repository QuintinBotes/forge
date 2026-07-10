"use client";

import { Cable, Cpu, KeyRound, Users, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface SettingsSection {
  label: string;
  href: string;
  icon: LucideIcon;
}

/** Every settings screen, in the order they appear in the shared section nav. */
export const SETTINGS_SECTIONS: SettingsSection[] = [
  { label: "Models & effort", href: "/settings/models", icon: Cpu },
  { label: "Access", href: "/settings/rbac", icon: Users },
  { label: "SSO", href: "/settings/sso", icon: KeyRound },
  { label: "Integrations", href: "/settings/integrations", icon: Cable },
];

function isActive(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Shared settings shell — a consistent section nav (Models & effort, Access,
 * SSO, Integrations) above every settings screen, so moving between them
 * doesn't require a trip back out to the primary sidebar. Each screen still
 * owns its own header, form cards and single primary "Save" action below.
 */
export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
      <nav
        aria-label="Settings sections"
        className="flex flex-wrap gap-1 rounded-lg border border-border bg-muted/40 p-1"
      >
        {SETTINGS_SECTIONS.map((section) => {
          const Icon = section.icon;
          const active = isActive(pathname, section.href);
          return (
            <Link
              key={section.href}
              href={section.href}
              aria-current={active ? "page" : undefined}
              data-testid={`settings-nav-${section.href.split("/").pop()}`}
              className={cn(
                "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                active
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              {section.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
