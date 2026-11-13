"use client";

import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useEffect, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
  subscribeThemePreference,
  systemPrefersDark,
  watchSystemTheme,
  type ThemePreference,
} from "@/lib/theme";

const NEXT: Record<ThemePreference, ThemePreference> = {
  system: "light",
  light: "dark",
  dark: "system",
};

const LABEL: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

const ICON: Record<ThemePreference, LucideIcon> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

/** Cycles the theme System → Light → Dark and remembers the choice. */
export function ThemeToggle() {
  // `null` on the server, which cannot know the stored choice; applying
  // "system" before reading it would flash the wrong palette.
  const preference = useSyncExternalStore(
    subscribeThemePreference,
    readThemePreference,
    () => null,
  );

  useEffect(() => {
    if (preference === null) {
      return;
    }
    applyTheme(resolveTheme(preference, systemPrefersDark()));
    if (preference !== "system") {
      return;
    }
    return watchSystemTheme((prefersDark) =>
      applyTheme(resolveTheme("system", prefersDark)),
    );
  }, [preference]);

  const current = preference ?? "system";
  const next = NEXT[current];
  const Icon = ICON[current];
  const label = `Theme: ${LABEL[current]}. Switch to ${LABEL[next]}.`;

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label={label}
      title={label}
      onClick={() => storeThemePreference(next)}
    >
      <Icon aria-hidden className="h-4 w-4" />
    </Button>
  );
}
