"use client";

import type { ReactNode } from "react";

import { CommandPaletteProvider } from "@/components/command-palette";
import { QueryProvider } from "@/components/query-provider";

/**
 * Client-side provider stack mounted once in the root layout: data fetching
 * (TanStack Query) wraps the global Cmd+K command palette.
 */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      <CommandPaletteProvider>{children}</CommandPaletteProvider>
    </QueryProvider>
  );
}
