"use client";

import {
  ArrowUpCircle,
  Boxes,
  Download,
  ExternalLink,
  GitBranch,
  Globe,
  History,
  Loader2,
  PackageOpen,
  Scale,
  Search,
  Store,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useInstallations,
  useListingDetail,
  useListings,
  useUpdateInstallation,
} from "@/lib/api/marketplace";
import type {
  ArtifactKind,
  Installation,
  Listing,
  ListingDetail,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  HashChip,
  KindGlyph,
  StatusBadge,
  TrustBadge,
  VerificationBadge,
} from "./marketplace-badges";
import { InstallDialog } from "./install-dialog";
import { filterListings, formatDate, kindLabel } from "./marketplace-meta";

type TabId = "browse" | "installed";
type KindFilter = ArtifactKind | "all";

const KIND_FILTERS: { id: KindFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "skill_profile", label: "Skill profiles" },
  { id: "mcp_connector", label: "MCP connectors" },
];

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

interface Selection {
  registrySlug: string;
  slug: string;
}

export interface MarketplaceViewProps {
  client?: ForgeApiClient;
}

/**
 * The Marketplace (F32): browse/search community skill profiles + MCP
 * connectors, inspect a package's manifest and verification provenance, and
 * install/update it. Trust is the spine — every package carries a registry
 * "hallmark" and every version its cryptographic assay. Keyboard-first: `/`
 * focuses search, the catalog is a grid of focusable cards, and the single
 * ember action installs the selected package.
 */
export function MarketplaceView({ client = apiClient }: MarketplaceViewProps) {
  const [tab, setTab] = useState<TabId>("browse");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [selected, setSelected] = useState<Selection | null>(null);
  const [installTarget, setInstallTarget] = useState<Listing | null>(null);

  const searchRef = useRef<HTMLInputElement>(null);

  const listingsQuery = useListings(
    kind === "all" ? undefined : { kind },
    client,
  );
  const listings = useMemo(
    () => listingsQuery.data ?? [],
    [listingsQuery.data],
  );
  const filtered = useMemo(
    () => filterListings(listings, query),
    [listings, query],
  );

  // Effective selection derived during render: honour the explicit pick when it
  // survives the current filter, else fall back to the first visible package.
  const effective: Selection | null = useMemo(() => {
    if (
      selected &&
      filtered.some(
        (l) =>
          l.registry_slug === selected.registrySlug && l.slug === selected.slug,
      )
    ) {
      return selected;
    }
    const first = filtered[0];
    return first
      ? { registrySlug: first.registry_slug, slug: first.slug }
      : null;
  }, [selected, filtered]);

  const detailQuery = useListingDetail(
    effective?.registrySlug ?? null,
    effective?.slug ?? null,
    client,
  );
  const selectedSummary = effective
    ? filtered.find(
        (l) =>
          l.registry_slug === effective.registrySlug &&
          l.slug === effective.slug,
      ) ?? null
    : null;
  const detail: ListingDetail | Listing | null =
    detailQuery.data ?? selectedSummary;

  const focusSearch = useCallback(() => {
    setTab("browse");
    // Defer so the browse panel (and its input) is mounted before we focus.
    requestAnimationFrame(() => searchRef.current?.focus());
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target)) return;
      if (event.key === "/") {
        event.preventDefault();
        focusSearch();
      }
    },
    [focusSearch],
  );

  // Command-palette contributions (stable refs → latest handlers).
  const focusSearchRef = useRef(focusSearch);
  useEffect(() => {
    focusSearchRef.current = focusSearch;
  }, [focusSearch]);
  const commands = useMemo(
    () => [
      {
        id: "marketplace-search",
        label: "Search marketplace",
        group: "Marketplace",
        icon: <Search />,
        shortcut: "/",
        run: () => focusSearchRef.current(),
      },
      {
        id: "marketplace-installed",
        label: "View installed packages",
        group: "Marketplace",
        icon: <Boxes />,
        run: () => setTab("installed"),
      },
    ],
    [],
  );
  useRegisterCommands("marketplace", commands);

  const onSelectTab = (next: TabId) => setTab(next);
  const onTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      setTab((t) => (t === "browse" ? "installed" : "browse"));
    }
  };

  return (
    <div
      data-testid="marketplace"
      role="region"
      aria-label="Marketplace"
      onKeyDown={onKeyDown}
      className="flex h-full flex-col gap-4 outline-none"
    >
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Store className="h-5 w-5 text-primary" aria-hidden />
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Marketplace
          </h1>
          <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
            {listings.length} {listings.length === 1 ? "package" : "packages"}
          </span>
        </div>

        <div
          role="tablist"
          aria-label="Marketplace views"
          onKeyDown={onTabKeyDown}
          className="inline-flex items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
        >
          {(["browse", "installed"] as TabId[]).map((id) => {
            const isActive = tab === id;
            return (
              <button
                key={id}
                role="tab"
                type="button"
                aria-selected={isActive}
                tabIndex={isActive ? 0 : -1}
                onClick={() => onSelectTab(id)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium capitalize transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  isActive
                    ? "bg-card text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {id}
              </button>
            );
          })}
        </div>
      </header>

      {tab === "browse" ? (
        <BrowsePanel
          searchRef={searchRef}
          query={query}
          onQuery={setQuery}
          kind={kind}
          onKind={(k) => {
            setKind(k);
            setSelected(null);
          }}
          listingsQuery={listingsQuery}
          filtered={filtered}
          effective={effective}
          onSelect={(l) =>
            setSelected({ registrySlug: l.registry_slug, slug: l.slug })
          }
          detail={detail}
          detailLoading={detailQuery.isLoading}
          onInstall={(l) => setInstallTarget(l)}
        />
      ) : (
        <InstalledPanel client={client} />
      )}

      {installTarget ? (
        <InstallDialog
          open={installTarget !== null}
          onOpenChange={(open) => {
            if (!open) setInstallTarget(null);
          }}
          listing={installTarget}
          client={client}
        />
      ) : null}
    </div>
  );
}

// --- Browse panel --------------------------------------------------------- //

interface BrowsePanelProps {
  searchRef: React.RefObject<HTMLInputElement | null>;
  query: string;
  onQuery: (q: string) => void;
  kind: KindFilter;
  onKind: (k: KindFilter) => void;
  listingsQuery: ReturnType<typeof useListings>;
  filtered: Listing[];
  effective: Selection | null;
  onSelect: (listing: Listing) => void;
  detail: ListingDetail | Listing | null;
  detailLoading: boolean;
  onInstall: (listing: Listing) => void;
}

function BrowsePanel({
  searchRef,
  query,
  onQuery,
  kind,
  onKind,
  listingsQuery,
  filtered,
  effective,
  onSelect,
  detail,
  detailLoading,
  onInstall,
}: BrowsePanelProps) {
  const isLoading = listingsQuery.isLoading;
  const hasListings = (listingsQuery.data?.length ?? 0) > 0;

  return (
    <div
      role="tabpanel"
      aria-label="Browse packages"
      className="flex min-h-0 flex-1 flex-col gap-4"
    >
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-56 flex-1">
          <Search
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          />
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="Search packages, tags, registries…  ( / )"
            aria-label="Search packages"
            className={cn(
              "h-10 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm",
              "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          />
        </div>
        <div
          role="group"
          aria-label="Filter by kind"
          className="inline-flex items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
        >
          {KIND_FILTERS.map((f) => {
            const isActive = kind === f.id;
            return (
              <button
                key={f.id}
                type="button"
                aria-pressed={isActive}
                onClick={() => onKind(f.id)}
                className={cn(
                  "rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  isActive
                    ? "bg-card text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[1fr_minmax(20rem,24rem)]">
        {/* Catalog */}
        <div className="min-h-0 overflow-y-auto">
          {isLoading ? (
            <CatalogSkeleton />
          ) : listingsQuery.isError ? (
            <CatalogError onRetry={() => listingsQuery.refetch()} />
          ) : !hasListings ? (
            <EmptyCatalog />
          ) : filtered.length === 0 ? (
            <EmptySearch query={query} />
          ) : (
            <ul
              aria-label="Packages"
              className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3"
            >
              {filtered.map((listing) => (
                <li key={listing.id}>
                  <PackageCard
                    listing={listing}
                    active={
                      effective?.registrySlug === listing.registry_slug &&
                      effective?.slug === listing.slug
                    }
                    onSelect={() => onSelect(listing)}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Detail rail */}
        <aside className="min-h-0 overflow-hidden rounded-lg border border-border bg-card lg:sticky lg:top-0">
          {detail ? (
            <PackageDetail
              listing={detail}
              loading={detailLoading}
              onInstall={() => onInstall(detail)}
            />
          ) : (
            <NoDetail loading={isLoading} />
          )}
        </aside>
      </div>
    </div>
  );
}

// --- Package card --------------------------------------------------------- //

function PackageCard({
  listing,
  active,
  onSelect,
}: {
  listing: Listing;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? "true" : undefined}
      data-testid={`package-card-${listing.slug}`}
      className={cn(
        "flex h-full w-full flex-col gap-3 rounded-lg border bg-card p-4 text-left transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "border-primary/50 bg-accent/60 shadow-sm"
          : "border-border hover:border-primary/30 hover:bg-accent/40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <KindGlyph kind={listing.kind} />
          </span>
          <div className="min-w-0">
            <p className="truncate font-display text-sm font-semibold text-foreground">
              {listing.name}
            </p>
            <p className="truncate text-[11px] text-muted-foreground">
              {kindLabel(listing.kind)}
            </p>
          </div>
        </div>
        <TrustBadge level={listing.trust_level} />
      </div>

      <p className="line-clamp-2 text-xs text-muted-foreground">
        {listing.summary}
      </p>

      <div className="mt-auto flex items-center justify-between gap-2 pt-1">
        <span className="truncate font-mono text-[11px] text-muted-foreground">
          {listing.registry_slug}
        </span>
        <span className="shrink-0 rounded border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
          v{listing.latest_version}
        </span>
      </div>
    </button>
  );
}

// --- Package detail ------------------------------------------------------- //

function hasVersions(l: ListingDetail | Listing): l is ListingDetail {
  return Array.isArray((l as ListingDetail).versions);
}

function PackageDetail({
  listing,
  loading,
  onInstall,
}: {
  listing: ListingDetail | Listing;
  loading: boolean;
  onInstall: () => void;
}) {
  const versions = hasVersions(listing) ? listing.versions : [];
  return (
    <div
      data-testid="package-detail"
      className="flex h-full flex-col overflow-y-auto"
    >
      <div className="flex flex-col gap-3 border-b border-border px-5 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
              <KindGlyph kind={listing.kind} className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h2 className="truncate font-display text-lg font-semibold leading-tight text-foreground">
                {listing.name}
              </h2>
              <p className="truncate text-xs text-muted-foreground">
                {kindLabel(listing.kind)} · {listing.slug}
              </p>
            </div>
          </div>
          <TrustBadge level={listing.trust_level} />
        </div>

        <p className="text-sm text-muted-foreground">{listing.summary}</p>

        <Button
          size="sm"
          onClick={onInstall}
          data-testid="install-package"
          className="w-full"
        >
          <Download className="h-4 w-4" aria-hidden />
          Install · v{listing.latest_version}
        </Button>
      </div>

      {/* Manifest facts */}
      <dl className="grid grid-cols-1 gap-px border-b border-border bg-border">
        <MetaRow icon={<Boxes className="h-3.5 w-3.5" />} label="Registry">
          <span className="font-mono text-xs">{listing.registry_slug}</span>
        </MetaRow>
        <MetaRow icon={<Scale className="h-3.5 w-3.5" />} label="License">
          {listing.license}
        </MetaRow>
        {listing.homepage ? (
          <MetaRow icon={<Globe className="h-3.5 w-3.5" />} label="Homepage">
            <ExternalLinkText href={listing.homepage} />
          </MetaRow>
        ) : null}
        {listing.repository ? (
          <MetaRow
            icon={<GitBranch className="h-3.5 w-3.5" />}
            label="Repository"
          >
            <ExternalLinkText href={listing.repository} />
          </MetaRow>
        ) : null}
      </dl>

      {listing.tags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 border-b border-border px-5 py-3">
          {listing.tags.map((t) => (
            <span
              key={t}
              className="rounded-full border border-border bg-muted/60 px-2 py-0.5 text-[11px] text-muted-foreground"
            >
              {t}
            </span>
          ))}
        </div>
      ) : null}

      {/* Version provenance */}
      <div className="flex flex-col gap-2 px-5 py-4">
        <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          <History className="h-3.5 w-3.5" aria-hidden />
          Versions
        </div>
        {loading && versions.length === 0 ? (
          <div
            data-testid="versions-skeleton"
            aria-busy="true"
            className="flex flex-col gap-2"
          >
            {[0, 1].map((i) => (
              <div
                key={i}
                className="h-10 animate-pulse rounded-md bg-muted/60"
              />
            ))}
          </div>
        ) : versions.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No published versions yet.
          </p>
        ) : (
          <ul className="flex flex-col gap-2" aria-label="Versions">
            {versions.map((v) => (
              <li
                key={v.version}
                className="flex flex-col gap-1.5 rounded-md border border-border bg-card/60 px-3 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs font-semibold text-foreground">
                    v{v.version}
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {formatDate(v.published_at)}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <VerificationBadge
                    status={v.signed ? "verified" : "unsigned"}
                  />
                  <HashChip hash={v.content_hash} />
                  {v.yanked ? (
                    <span
                      data-testid={`yanked-${v.version}`}
                      className="rounded-full border border-danger/40 bg-danger/10 px-2 py-0.5 text-[11px] font-medium text-danger"
                    >
                      Yanked
                    </span>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function MetaRow({
  icon,
  label,
  children,
}: {
  icon: ReactNode;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 bg-card px-5 py-2.5 text-sm">
      <dt className="flex items-center gap-2 text-muted-foreground">
        {icon}
        {label}
      </dt>
      <dd className="min-w-0 truncate text-right text-foreground">{children}</dd>
    </div>
  );
}

function ExternalLinkText({ href }: { href: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="inline-flex items-center gap-1 truncate text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="truncate">{href.replace(/^https?:\/\//, "")}</span>
      <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
    </a>
  );
}

// --- Installed panel ------------------------------------------------------ //

function InstalledPanel({ client }: { client: ForgeApiClient }) {
  const installationsQuery = useInstallations(client);
  const update = useUpdateInstallation(client);
  const installations = installationsQuery.data ?? [];

  return (
    <div
      role="tabpanel"
      aria-label="Installed packages"
      className="flex min-h-0 flex-1 flex-col"
    >
      {installationsQuery.isLoading ? (
        <InstalledSkeleton />
      ) : installationsQuery.isError ? (
        <ErrorState
          data-testid="installed-error"
          title="Installed packages are unavailable"
          description="The marketplace service may be offline. Check your connection and try again."
          onRetry={() => installationsQuery.refetch()}
        />
      ) : installations.length === 0 ? (
        <EmptyInstalled />
      ) : (
        <ul aria-label="Installed packages" className="flex flex-col gap-2">
          {installations.map((inst) => (
            <InstalledRow
              key={inst.id}
              installation={inst}
              onUpdate={() =>
                update.mutate(
                  {
                    installationId: inst.id,
                    version: inst.available_version ?? undefined,
                  },
                  {
                    onSuccess: () =>
                      toast.success(
                        `Updated ${inst.listing_slug}${
                          inst.available_version ? ` to v${inst.available_version}` : ""
                        }`,
                      ),
                  },
                )
              }
              updating={
                update.isPending && update.variables?.installationId === inst.id
              }
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function InstalledRow({
  installation,
  onUpdate,
  updating,
}: {
  installation: Installation;
  onUpdate: () => void;
  updating: boolean;
}) {
  const hasUpdate = installation.status === "update_available";
  return (
    <li
      data-testid={`installation-${installation.listing_slug}`}
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3"
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <span className="truncate font-display text-sm font-semibold text-foreground">
            {installation.listing_slug}
          </span>
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {installation.registry_slug}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={installation.status} />
          <VerificationBadge status={installation.verification_status} />
          <span className="font-mono text-[11px] text-muted-foreground">
            v{installation.installed_version}
            {hasUpdate && installation.available_version
              ? ` → v${installation.available_version}`
              : ""}
          </span>
        </div>
      </div>

      {hasUpdate ? (
        <Button
          size="sm"
          onClick={onUpdate}
          disabled={updating}
          data-testid={`update-${installation.listing_slug}`}
        >
          {updating ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <ArrowUpCircle className="h-4 w-4" aria-hidden />
          )}
          {updating
            ? "Updating…"
            : `Update to v${installation.available_version}`}
        </Button>
      ) : (
        <span className="text-[11px] text-muted-foreground">Up to date</span>
      )}
    </li>
  );
}

// --- Empty / loading states ----------------------------------------------- //

function CatalogSkeleton() {
  return (
    <Loading
      data-testid="catalog-skeleton"
      label="Loading packages…"
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3"
    >
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          className="flex h-36 flex-col gap-3 rounded-lg border border-border bg-card p-4"
        >
          <Skeleton className="h-8 w-8" />
          <Skeleton className="h-3.5 w-2/3" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="mt-auto h-3 w-1/3" />
        </div>
      ))}
    </Loading>
  );
}

function InstalledSkeleton() {
  return (
    <Loading data-testid="installed-skeleton" label="Loading installed packages…" className="flex flex-col gap-2">
      {[0, 1, 2].map((i) => (
        <Skeleton key={i} className="h-16 rounded-lg" />
      ))}
    </Loading>
  );
}

function EmptyCatalog() {
  return (
    <EmptyState
      data-testid="empty-catalog"
      icon={<PackageOpen />}
      title="No packages yet"
      description="Add and sync a registry to populate the catalog with community skill profiles and MCP connectors."
    />
  );
}

function EmptySearch({ query }: { query: string }) {
  return (
    <EmptyState
      data-testid="empty-search"
      icon={<Search />}
      title="No packages match"
      description={`Nothing matches "${query}". Try a different term or clear the search.`}
    />
  );
}

function EmptyInstalled() {
  return (
    <EmptyState
      data-testid="empty-installed"
      icon={<Boxes />}
      title="Nothing installed yet"
      description="Browse the catalog and install a package to see it here."
    />
  );
}

function CatalogError({ onRetry }: { onRetry?: () => void }) {
  return (
    <ErrorState
      data-testid="catalog-error"
      title="Catalog unavailable"
      description="The marketplace registry is unreachable — check back shortly."
      onRetry={onRetry}
    />
  );
}

function NoDetail({ loading }: { loading: boolean }) {
  return (
    <EmptyState
      icon={<PackageOpen />}
      title={loading ? "Loading the catalog…" : "Select a package"}
      description={loading ? undefined : "Inspect its manifest and verification, then install it."}
      className="h-full border-none bg-transparent"
    />
  );
}
