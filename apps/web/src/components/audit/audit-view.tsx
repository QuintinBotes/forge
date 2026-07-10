"use client";

import {
  Download,
  ListFilter,
  RotateCw,
  ScrollText,
  Search,
  ShieldCheck,
  X,
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
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useAuditLog,
  useAuditVocabulary,
  useVerifyAuditChain,
  type AuditFilters,
} from "@/lib/api/audit";
import {
  AUDIT_ACTOR_TYPES,
  AUDIT_OUTCOMES,
  AUDIT_SEVERITIES,
  type AuditEntry,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { OutcomeBadge, SeverityBadge } from "./audit-badges";
import { AuditDetailDrawer } from "./audit-detail-drawer";
import {
  actionLabel,
  actionMeta,
  actorDisplay,
  actorTypeMeta,
  humanize,
  presetToFrom,
  relativeTime,
  absoluteTime,
  shortId,
  TIME_PRESETS,
} from "./audit-meta";

type SelectKey = "actor_type" | "action" | "target_type" | "outcome" | "severity";

const EMPTY_SELECTS: Record<SelectKey, string> = {
  actor_type: "",
  action: "",
  target_type: "",
  outcome: "",
  severity: "",
};

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function downloadText(text: string, filename: string): void {
  if (typeof document === "undefined") return;
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return; // jsdom / SSR: no object URLs — the status announcement still fires.
  }
  const blob = new Blob([text], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function uniqueSorted(values: (string | null | undefined)[]): string[] {
  return Array.from(
    new Set(values.filter((v): v is string => Boolean(v))),
  ).sort();
}

export interface AuditViewProps {
  client?: ForgeApiClient;
}

/**
 * The audit viewer — a queryable window onto the immutable, hash-chained,
 * secret-redacted audit log. A filter toolbar (actor / action / resource /
 * outcome / severity / time) drives a cursor-paginated table; selecting a row
 * opens the detail drawer. Keyboard-first: `j`/`k` move the cursor, `Enter`
 * opens, `/` focuses search, `e` exports, `v` verifies the chain. Export is the
 * single ember action; chain verification is the steel companion.
 */
export function AuditView({ client = apiClient }: AuditViewProps) {
  const [selects, setSelects] = useState<Record<SelectKey, string>>(EMPTY_SELECTS);
  const [searchDraft, setSearchDraft] = useState("");
  const [committedSearch, setCommittedSearch] = useState("");
  const [timeRange, setTimeRange] = useState("all");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [openEntry, setOpenEntry] = useState<AuditEntry | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const searchRef = useRef<HTMLInputElement>(null);

  // Debounce the free-text search so each keystroke doesn't refetch.
  useEffect(() => {
    const timer = setTimeout(() => setCommittedSearch(searchDraft.trim()), 200);
    return () => clearTimeout(timer);
  }, [searchDraft]);

  // Capture the lower bound once per range change (stable across renders so the
  // query key doesn't churn every tick).
  const from = useMemo(() => presetToFrom(timeRange), [timeRange]);

  const query = useMemo<AuditFilters>(() => {
    const built: AuditFilters = { limit: 50 };
    if (selects.actor_type) built.actor_type = selects.actor_type;
    if (selects.action) built.action = selects.action;
    if (selects.target_type) built.target_type = selects.target_type;
    if (selects.outcome) built.outcome = selects.outcome;
    if (selects.severity) built.severity = selects.severity;
    if (committedSearch) built.q = committedSearch;
    if (from) built.from = from;
    return built;
  }, [selects, committedSearch, from]);

  const listQuery = useAuditLog(query, client);
  const vocabQuery = useAuditVocabulary(client);
  const verify = useVerifyAuditChain(client);

  const entries = useMemo(
    () => listQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [listQuery.data],
  );

  const hasActiveFilters =
    Object.values(selects).some(Boolean) ||
    committedSearch.length > 0 ||
    timeRange !== "all";

  // Option lists: prefer the server vocabulary, else derive from the rows.
  const vocab = vocabQuery.data;
  const actionOptions = vocab?.actions ?? uniqueSorted(entries.map((e) => e.action));
  const actorTypeOptions = vocab?.actor_types ?? [...AUDIT_ACTOR_TYPES];
  const resourceOptions =
    vocab?.resource_types ?? uniqueSorted(entries.map((e) => e.target_type));
  const outcomeOptions = vocab?.outcomes ?? [...AUDIT_OUTCOMES];
  const severityOptions = vocab?.severities ?? [...AUDIT_SEVERITIES];

  const safeIndex = entries.length
    ? Math.min(selectedIndex, entries.length - 1)
    : 0;

  const openDrawer = useCallback((entry: AuditEntry, index: number) => {
    setSelectedIndex(index);
    setOpenEntry(entry);
    setDrawerOpen(true);
  }, []);

  const moveSelection = useCallback(
    (delta: number) => {
      setSelectedIndex((prev) => {
        if (entries.length === 0) return 0;
        const base = Math.min(prev, entries.length - 1);
        return Math.min(Math.max(base + delta, 0), entries.length - 1);
      });
    },
    [entries.length],
  );

  const setSelect = useCallback((key: SelectKey, value: string) => {
    setSelects((prev) => ({ ...prev, [key]: value }));
    setSelectedIndex(0);
  }, []);

  const clearFilters = useCallback(() => {
    setSelects(EMPTY_SELECTS);
    setSearchDraft("");
    setCommittedSearch("");
    setTimeRange("all");
    setSelectedIndex(0);
  }, []);

  const onExport = useCallback(async () => {
    setExporting(true);
    setStatus(null);
    try {
      const ndjson = await client.exportAuditNdjson({ from: query.from, to: query.to });
      const count = ndjson.trim() ? ndjson.trim().split("\n").length : 0;
      downloadText(ndjson, "audit-export.ndjson");
      const message = `Exported ${count} ${count === 1 ? "entry" : "entries"} as NDJSON.`;
      setStatus(message);
      toast.success(message);
    } catch {
      const message = "Export failed — please try again.";
      setStatus(message);
      toast.error(message);
    } finally {
      setExporting(false);
    }
  }, [client, query.from, query.to]);

  const onVerify = useCallback(() => {
    if (verify.isPending) return;
    setStatus(null);
    verify.mutate(undefined, {
      onSuccess: (result) => {
        const message = result.ok
          ? `Chain verified — ${result.entries_checked} entries intact.`
          : `Chain integrity broken at entry #${result.broken_at_seq}.`;
        setStatus(message);
        if (result.ok) toast.success(message);
        else toast.error(message);
      },
      onError: () => {
        const message = "Couldn't verify the chain right now.";
        setStatus(message);
        toast.error(message);
      },
    });
  }, [verify]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (drawerOpen || isEditableTarget(event.target)) return;
      switch (event.key) {
        case "j":
        case "ArrowDown":
          event.preventDefault();
          moveSelection(1);
          return;
        case "k":
        case "ArrowUp":
          event.preventDefault();
          moveSelection(-1);
          return;
        case "Enter":
          if (entries[safeIndex]) {
            event.preventDefault();
            openDrawer(entries[safeIndex], safeIndex);
          }
          return;
        case "/":
          event.preventDefault();
          searchRef.current?.focus();
          return;
        case "e":
          event.preventDefault();
          void onExport();
          return;
        case "v":
          event.preventDefault();
          onVerify();
          return;
        default:
      }
    },
    [drawerOpen, moveSelection, entries, safeIndex, openDrawer, onExport, onVerify],
  );

  // Command-palette contributions (stable refs → latest handlers).
  const exportRef = useRef(onExport);
  const verifyRef = useRef(onVerify);
  useEffect(() => {
    exportRef.current = onExport;
    verifyRef.current = onVerify;
  }, [onExport, onVerify]);
  const commands = useMemo(
    () => [
      {
        id: "audit-export",
        label: "Export audit log (NDJSON)",
        group: "Audit",
        icon: <Download />,
        shortcut: "E",
        run: () => void exportRef.current(),
      },
      {
        id: "audit-verify",
        label: "Verify audit chain",
        group: "Audit",
        icon: <ShieldCheck />,
        shortcut: "V",
        run: () => verifyRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("audit", commands);

  const isInitialLoading = listQuery.isLoading && entries.length === 0;
  const isEmpty = !listQuery.isLoading && !listQuery.isError && entries.length === 0;
  const countLabel = `${entries.length}${listQuery.hasNextPage ? "+" : ""}`;
  const verdict = verify.data;

  return (
    <div
      data-testid="audit-view"
      role="region"
      aria-label="Audit log"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full flex-col gap-4 outline-none"
    >
      {/* Header */}
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-3">
            <h1 className="flex items-center gap-2 font-display text-xl font-semibold tracking-tight">
              <ScrollText aria-hidden className="h-5 w-5 text-primary" />
              Audit log
            </h1>
            <span
              data-testid="audit-count"
              className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {countLabel} shown
            </span>
          </div>
          <p className="text-sm text-muted-foreground">
            Immutable, hash-chained record of every action. Read-only and
            secret-redacted.
          </p>
        </div>

        <div className="flex items-center gap-2">
          {verdict ? (
            <span
              data-testid="chain-verdict"
              className={cn(
                "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium",
                verdict.ok
                  ? "border-success/40 bg-success/10 text-success"
                  : "border-danger/40 bg-danger/10 text-danger",
              )}
            >
              <ShieldCheck aria-hidden className="h-3.5 w-3.5" />
              {verdict.ok
                ? `Chain intact (${verdict.entries_checked})`
                : `Broken at #${verdict.broken_at_seq}`}
            </span>
          ) : null}
          <Button variant="outline" onClick={onVerify} disabled={verify.isPending}>
            <ShieldCheck aria-hidden className="h-4 w-4" />
            {verify.isPending ? "Verifying…" : "Verify chain"}
          </Button>
          <Button
            data-testid="export-ndjson"
            onClick={() => void onExport()}
            disabled={exporting}
          >
            <Download aria-hidden className="h-4 w-4" />
            {exporting ? "Exporting…" : "Export"}
          </Button>
        </div>
      </header>

      {/* Toolbar */}
      <div className="flex flex-col gap-3 rounded-lg border border-border bg-card/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[16rem] flex-1">
            <Search
              aria-hidden
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            />
            <input
              ref={searchRef}
              type="search"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  setCommittedSearch(searchDraft.trim());
                }
              }}
              aria-label="Search audit log"
              placeholder="Search actor, reason, details…"
              className="h-9 w-full rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          {hasActiveFilters ? (
            <Button
              variant="outline"
              size="sm"
              data-testid="clear-filters"
              onClick={clearFilters}
              className="text-muted-foreground"
            >
              <X aria-hidden className="h-3.5 w-3.5" />
              Clear
            </Button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <ListFilter aria-hidden className="h-3.5 w-3.5" />
            Filters
          </span>
          <FilterSelect
            label="Action"
            value={selects.action}
            options={actionOptions}
            format={actionLabel}
            onChange={(value) => setSelect("action", value)}
          />
          <FilterSelect
            label="Actor type"
            value={selects.actor_type}
            options={actorTypeOptions}
            format={(v) => actorTypeMeta(v).label}
            onChange={(value) => setSelect("actor_type", value)}
          />
          <FilterSelect
            label="Resource"
            value={selects.target_type}
            options={resourceOptions}
            format={humanize}
            onChange={(value) => setSelect("target_type", value)}
          />
          <FilterSelect
            label="Outcome"
            value={selects.outcome}
            options={outcomeOptions}
            format={humanize}
            onChange={(value) => setSelect("outcome", value)}
          />
          <FilterSelect
            label="Severity"
            value={selects.severity}
            options={severityOptions}
            format={humanize}
            onChange={(value) => setSelect("severity", value)}
          />
          <FilterSelect
            label="Time"
            value={timeRange}
            options={TIME_PRESETS.map((p) => p.value)}
            format={(v) => TIME_PRESETS.find((p) => p.value === v)?.label ?? v}
            includeAll={false}
            onChange={(value) => {
              setTimeRange(value);
              setSelectedIndex(0);
            }}
          />
        </div>
      </div>

      {/* Live region for export / verify announcements. */}
      <p data-testid="audit-status" role="status" aria-live="polite" className="sr-only">
        {status}
      </p>

      {/* Table / states */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card">
        {listQuery.isError ? (
          <ErrorState onRetry={() => void listQuery.refetch()} />
        ) : isInitialLoading ? (
          <TableSkeleton />
        ) : isEmpty ? (
          hasActiveFilters ? (
            <EmptyFiltered onClear={clearFilters} />
          ) : (
            <EmptyAudit />
          )
        ) : (
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="w-full min-w-[52rem] border-collapse text-sm">
              <thead className="sticky top-0 z-10 bg-card">
                <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <Th>Time</Th>
                  <Th>Actor</Th>
                  <Th>Action</Th>
                  <Th>Resource</Th>
                  <Th>Outcome</Th>
                  <Th>Severity</Th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry, index) => (
                  <AuditRow
                    key={entry.id}
                    entry={entry}
                    selected={index === safeIndex}
                    onSelect={() => openDrawer(entry, index)}
                  />
                ))}
              </tbody>
            </table>

            {listQuery.hasNextPage ? (
              <div className="flex justify-center border-t border-border p-3">
                <Button
                  variant="outline"
                  data-testid="load-more"
                  onClick={() => void listQuery.fetchNextPage()}
                  disabled={listQuery.isFetchingNextPage}
                >
                  {listQuery.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <AuditDetailDrawer
        entry={openEntry}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
      />
    </div>
  );
}

// --- Row ------------------------------------------------------------------ //

function AuditRow({
  entry,
  selected,
  onSelect,
}: {
  entry: AuditEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  const ActionIcon = actionMeta(entry.action).icon;
  const ActorIcon = actorTypeMeta(entry.actor_type).icon;
  return (
    <tr
      data-testid="audit-row"
      aria-selected={selected}
      onClick={onSelect}
      className={cn(
        "cursor-pointer border-b border-border/60 transition-colors hover:bg-accent/60",
        selected && "bg-accent",
      )}
    >
      <td className="whitespace-nowrap px-3 py-2.5 text-xs text-muted-foreground" title={absoluteTime(entry.created_at)}>
        {relativeTime(entry.created_at)}
      </td>
      <td className="px-3 py-2.5">
        <span className="flex items-center gap-1.5">
          <ActorIcon aria-hidden className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate text-foreground">{actorDisplay(entry)}</span>
        </span>
      </td>
      <td className="px-3 py-2.5">
        <span className="flex items-center gap-1.5">
          <ActionIcon aria-hidden className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate font-medium text-foreground">
            {actionLabel(entry.action)}
          </span>
          <code className="hidden shrink-0 rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground lg:inline">
            {entry.action}
          </code>
        </span>
      </td>
      <td className="px-3 py-2.5 text-xs">
        {entry.target_type ? (
          <span className="text-foreground">
            {humanize(entry.target_type)}
            {entry.target_id ? (
              <span className="ml-1.5 font-mono text-muted-foreground">
                {shortId(entry.target_id)}
              </span>
            ) : null}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2.5">
        <OutcomeBadge result={entry.result} />
      </td>
      <td className="px-3 py-2.5">
        <SeverityBadge severity={entry.severity} />
      </td>
    </tr>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-medium">{children}</th>;
}

// --- Filter select -------------------------------------------------------- //

function FilterSelect({
  label,
  value,
  options,
  format,
  onChange,
  includeAll = true,
}: {
  label: string;
  value: string;
  options: string[];
  format: (value: string) => string;
  onChange: (value: string) => void;
  includeAll?: boolean;
}) {
  const active = includeAll ? value !== "" : false;
  return (
    <label className="inline-flex items-center">
      <span className="sr-only">{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn(
          "h-8 rounded-md border bg-background px-2 text-xs text-foreground transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          active
            ? "border-primary/40 bg-accent text-accent-foreground"
            : "border-border",
        )}
      >
        {includeAll ? <option value="">{label}: all</option> : null}
        {options.map((option) => (
          <option key={option} value={option}>
            {includeAll ? `${label}: ` : ""}
            {format(option)}
          </option>
        ))}
      </select>
    </label>
  );
}

// --- States --------------------------------------------------------------- //

function TableSkeleton() {
  return (
    <div data-testid="audit-skeleton" aria-busy="true" className="flex flex-col">
      {[0, 1, 2, 3, 4, 5, 6].map((row) => (
        <div
          key={row}
          className="flex items-center gap-4 border-b border-border/60 px-3 py-3"
        >
          <div className="h-3 w-16 animate-pulse rounded bg-muted" />
          <div className="h-3 w-32 animate-pulse rounded bg-muted/70" />
          <div className="h-3 w-40 animate-pulse rounded bg-muted" />
          <div className="h-3 w-24 animate-pulse rounded bg-muted/70" />
          <div className="ml-auto h-4 w-16 animate-pulse rounded-full bg-muted/70" />
        </div>
      ))}
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="audit-error"
      className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center"
    >
      <ScrollText aria-hidden className="h-8 w-8 text-muted-foreground" />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">
          The audit log is unavailable
        </p>
        <p className="text-xs text-muted-foreground">
          We couldn&apos;t load audit entries just now.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry}>
        <RotateCw aria-hidden className="h-3.5 w-3.5" />
        Retry
      </Button>
    </div>
  );
}

function EmptyFiltered({ onClear }: { onClear: () => void }) {
  return (
    <div
      data-testid="empty-filtered"
      className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center"
    >
      <ListFilter aria-hidden className="h-8 w-8 text-muted-foreground" />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">No matching entries</p>
        <p className="text-xs text-muted-foreground">
          No audit activity matches these filters.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onClear}>
        <X aria-hidden className="h-3.5 w-3.5" />
        Clear filters
      </Button>
    </div>
  );
}

function EmptyAudit() {
  return (
    <div
      data-testid="empty-audit"
      className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center"
    >
      <ScrollText aria-hidden className="h-8 w-8 text-muted-foreground" />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">No audit activity yet</p>
        <p className="text-xs text-muted-foreground">
          Actions across the workspace — agent runs, approvals, secret access —
          will appear here as they happen.
        </p>
      </div>
    </div>
  );
}
