"use client";

import type { ReactNode } from "react";

import { CommandPaletteProvider } from "@/components/command-palette";
import { QueryProvider } from "@/components/query-provider";
import { Toaster } from "@/components/ui/toast";

/**
 * Client-side provider stack mounted once in the root layout: data fetching
 * (TanStack Query) wraps the global Cmd+K command palette, plus the single
 * toast stack every screen's action confirmations render into.
 */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      <CommandPaletteProvider>
        {children}
        <Toaster />
      </CommandPaletteProvider>
    </QueryProvider>
  );
}
