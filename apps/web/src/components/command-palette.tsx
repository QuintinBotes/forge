"use client";

import {
  AlertTriangle,
  KanbanSquare,
  LayoutList,
  Plus,
  Search,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";

interface CommandPaletteContextValue {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

const CommandPaletteContext = createContext<CommandPaletteContextValue | null>(null);

/** Open/close the command palette from anywhere (e.g. a top-bar button). */
export function useCommandPalette(): CommandPaletteContextValue {
  const ctx = useContext(CommandPaletteContext);
  if (!ctx) {
    throw new Error("useCommandPalette must be used within <CommandPaletteProvider>");
  }
  return ctx;
}

export interface CommandAction {
  id: string;
  label: string;
  icon?: ReactNode;
  shortcut?: string;
  group: string;
  run: (router: ReturnType<typeof useRouter>) => void;
}

/**
 * The Phase-0 command set. Task 1.6 replaces/extends these with real board
 * actions (create task, change status, assign, search...). Each action receives
 * the router so navigation works out of the box.
 */
export const DEFAULT_COMMANDS: CommandAction[] = [
  {
    id: "create-task",
    label: "Create task",
    group: "Create",
    icon: <Plus />,
    shortcut: "C",
    run: () => {
      /* wired in Task 1.6 */
    },
  },
  {
    id: "search",
    label: "Search knowledge",
    group: "Create",
    icon: <Search />,
    shortcut: "/",
    run: () => {
      /* wired in Task 1.6 */
    },
  },
  {
    id: "go-list",
    label: "Go to List",
    group: "Navigate",
    icon: <LayoutList />,
    run: (router) => router.push("/"),
  },
  {
    id: "go-board",
    label: "Go to Board",
    group: "Navigate",
    icon: <KanbanSquare />,
    run: (router) => router.push("/board"),
  },
  {
    id: "go-incidents",
    label: "Go to Incidents",
    group: "Navigate",
    icon: <AlertTriangle />,
    run: (router) => router.push("/incidents"),
  },
];

export interface CommandPaletteProviderProps {
  children: ReactNode;
  commands?: CommandAction[];
}

export function CommandPaletteProvider({
  children,
  commands = DEFAULT_COMMANDS,
}: CommandPaletteProviderProps) {
  const [open, setOpen] = useState(false);
  const router = useRouter();

  const toggle = useCallback(() => setOpen((value) => !value), []);

  // Cmd+K / Ctrl+K toggles the palette globally.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        toggle();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [toggle]);

  const value = useMemo(
    () => ({ open, setOpen, toggle }),
    [open, toggle],
  );

  const grouped = useMemo(() => {
    const groups = new Map<string, CommandAction[]>();
    for (const command of commands) {
      const list = groups.get(command.group) ?? [];
      list.push(command);
      groups.set(command.group, list);
    }
    return Array.from(groups.entries());
  }, [commands]);

  return (
    <CommandPaletteContext.Provider value={value}>
      {children}
      <CommandDialog open={open} onOpenChange={setOpen}>
        <CommandInput placeholder="Type a command or search…" />
        <CommandList>
          <CommandEmpty>No results found.</CommandEmpty>
          {grouped.map(([group, actions], index) => (
            <div key={group}>
              {index > 0 ? <CommandSeparator /> : null}
              <CommandGroup heading={group}>
                {actions.map((action) => (
                  <CommandItem
                    key={action.id}
                    value={action.label}
                    onSelect={() => {
                      setOpen(false);
                      action.run(router);
                    }}
                  >
                    {action.icon}
                    <span>{action.label}</span>
                    {action.shortcut ? (
                      <CommandShortcut>{action.shortcut}</CommandShortcut>
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            </div>
          ))}
        </CommandList>
      </CommandDialog>
    </CommandPaletteContext.Provider>
  );
}
