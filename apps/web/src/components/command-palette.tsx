"use client";

import {
  AlertTriangle,
  KanbanSquare,
  Layers,
  LayoutList,
  Plus,
  Route,
  ScrollText,
  Search,
  ShieldCheck,
  Store,
  TrendingUp,
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
  /** Register a named set of commands (replaces any prior set with that id). */
  registerCommands: (id: string, commands: CommandAction[]) => void;
  unregisterCommands: (id: string) => void;
}

const CommandPaletteContext = createContext<CommandPaletteContextValue | null>(null);

/** Open/close the command palette, or (un)register dynamic commands. */
export function useCommandPalette(): CommandPaletteContextValue {
  const ctx = useContext(CommandPaletteContext);
  if (!ctx) {
    throw new Error("useCommandPalette must be used within <CommandPaletteProvider>");
  }
  return ctx;
}

/**
 * Register a set of page-scoped commands while the calling component is mounted
 * (e.g. the board view contributes "Create task" / "Search knowledge"). Pass a
 * **stable** `commands` reference (memoize it) to avoid re-registration loops.
 */
export function useRegisterCommands(id: string, commands: CommandAction[]): void {
  const { registerCommands, unregisterCommands } = useCommandPalette();
  useEffect(() => {
    registerCommands(id, commands);
    return () => unregisterCommands(id);
  }, [id, commands, registerCommands, unregisterCommands]);
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
 * Always-available navigation/create commands. Page-scoped behavioural commands
 * (create-task wired to the API, etc.) are contributed via
 * {@link useRegisterCommands}.
 */
export const DEFAULT_COMMANDS: CommandAction[] = [
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
    id: "go-depth",
    label: "Go to Board depth",
    group: "Navigate",
    icon: <Layers />,
    run: (router) => router.push("/depth"),
  },
  {
    id: "go-sprints",
    label: "Go to Sprints",
    group: "Navigate",
    icon: <TrendingUp />,
    run: (router) => router.push("/sprints"),
  },
  {
    id: "go-incidents",
    label: "Go to Incidents",
    group: "Navigate",
    icon: <AlertTriangle />,
    run: (router) => router.push("/incidents"),
  },
  {
    id: "go-specs",
    label: "Go to Specs",
    group: "Navigate",
    icon: <Route />,
    run: (router) => router.push("/specs"),
  },
  {
    id: "go-approvals",
    label: "Go to Approvals",
    group: "Navigate",
    icon: <ShieldCheck />,
    run: (router) => router.push("/approvals"),
  },
  {
    id: "go-audit",
    label: "Go to Audit log",
    group: "Navigate",
    icon: <ScrollText />,
    run: (router) => router.push("/audit"),
  },
  {
    id: "go-marketplace",
    label: "Go to Marketplace",
    group: "Navigate",
    icon: <Store />,
    run: (router) => router.push("/marketplace"),
  },
];

export interface BuildBoardCommandsOptions {
  onCreateTask: () => void;
  onSearch?: () => void;
}

/** Board-scoped command set, parameterised by the page's callbacks. */
export function buildBoardCommands({
  onCreateTask,
  onSearch,
}: BuildBoardCommandsOptions): CommandAction[] {
  const commands: CommandAction[] = [
    {
      id: "create-task",
      label: "Create task",
      group: "Create",
      icon: <Plus />,
      shortcut: "C",
      run: () => onCreateTask(),
    },
  ];
  if (onSearch) {
    commands.push({
      id: "search-knowledge",
      label: "Search knowledge",
      group: "Create",
      icon: <Search />,
      shortcut: "/",
      run: () => onSearch(),
    });
  }
  return commands;
}

export interface CommandPaletteProviderProps {
  children: ReactNode;
  /** Base commands (defaults to navigation/create). */
  commands?: CommandAction[];
}

export function CommandPaletteProvider({
  children,
  commands = DEFAULT_COMMANDS,
}: CommandPaletteProviderProps) {
  const [open, setOpen] = useState(false);
  const [dynamic, setDynamic] = useState<Record<string, CommandAction[]>>({});
  const router = useRouter();

  const toggle = useCallback(() => setOpen((value) => !value), []);

  const registerCommands = useCallback((id: string, next: CommandAction[]) => {
    setDynamic((prev) => ({ ...prev, [id]: next }));
  }, []);

  const unregisterCommands = useCallback((id: string) => {
    setDynamic((prev) => {
      if (!(id in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

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
    () => ({ open, setOpen, toggle, registerCommands, unregisterCommands }),
    [open, toggle, registerCommands, unregisterCommands],
  );

  const allCommands = useMemo(
    () => [...commands, ...Object.values(dynamic).flat()],
    [commands, dynamic],
  );

  const grouped = useMemo(() => {
    const groups = new Map<string, CommandAction[]>();
    for (const command of allCommands) {
      const list = groups.get(command.group) ?? [];
      list.push(command);
      groups.set(command.group, list);
    }
    return Array.from(groups.entries());
  }, [allCommands]);

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
