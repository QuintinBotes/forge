import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type {
  Installation,
  Listing,
  ListingDetail,
  Registry,
} from "@/lib/api/types";

import { MarketplaceView } from "./marketplace-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const skill: Listing = {
  id: "l1",
  registry_id: "reg-1",
  registry_slug: "forge-official",
  trust_level: "official",
  kind: "skill_profile",
  slug: "python-pro",
  name: "Python Pro",
  summary: "Expert Python engineering profile with test-first defaults.",
  tags: ["python", "backend"],
  latest_version: "1.2.0",
  license: "Apache-2.0",
  cached_at: "2026-01-01T00:00:00Z",
};

const connector: Listing = {
  id: "l2",
  registry_id: "reg-2",
  registry_slug: "acme-registry",
  trust_level: "community",
  kind: "mcp_connector",
  slug: "github",
  name: "GitHub Connector",
  summary: "Read and write GitHub issues and pull requests.",
  tags: ["github", "vcs"],
  latest_version: "0.9.1",
  license: "MIT",
  cached_at: "2026-01-01T00:00:00Z",
};

const LISTINGS = [skill, connector];

const skillDetail: ListingDetail = {
  ...skill,
  homepage: "https://example.com/python-pro",
  repository: "https://github.com/acme/python-pro",
  versions: [
    {
      version: "1.2.0",
      content_hash: `sha256:${"a".repeat(64)}`,
      signed: true,
      published_at: "2026-02-01T00:00:00Z",
      yanked: false,
    },
    {
      version: "1.1.0",
      content_hash: `sha256:${"b".repeat(64)}`,
      signed: false,
      published_at: "2026-01-10T00:00:00Z",
      yanked: true,
      yanked_reason: "Regression",
    },
  ],
};

const connectorDetail: ListingDetail = {
  ...connector,
  versions: [
    {
      version: "0.9.1",
      content_hash: `sha256:${"c".repeat(64)}`,
      signed: false,
      published_at: "2026-02-05T00:00:00Z",
    },
  ],
};

const INSTALLATIONS: Installation[] = [
  {
    id: "inst-1",
    registry_slug: "forge-official",
    listing_slug: "python-pro",
    kind: "skill_profile",
    installed_version: "1.1.0",
    pinned: false,
    target_kind: "skill_profile",
    content_hash: `sha256:${"a".repeat(64)}`,
    verification_status: "verified",
    status: "update_available",
    available_version: "1.2.0",
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "inst-2",
    registry_slug: "acme-registry",
    listing_slug: "github",
    kind: "mcp_connector",
    installed_version: "0.9.1",
    pinned: false,
    target_kind: "mcp_connection",
    content_hash: `sha256:${"c".repeat(64)}`,
    verification_status: "unsigned",
    status: "installed",
    created_at: "2026-01-01T00:00:00Z",
  },
];

const REGISTRIES: Registry[] = [
  {
    id: "reg-2",
    slug: "acme-registry",
    name: "Acme Registry",
    type: "http_index",
    url: "https://registry.acme.test/index.json",
    trust_level: "community",
    enabled: true,
    has_public_key: false,
    created_at: "2026-01-01T00:00:00Z",
  },
];

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listListings: vi.fn(() => Promise.resolve(LISTINGS)),
    getListing: vi.fn((registrySlug: string, slug: string) =>
      Promise.resolve(slug === "github" ? connectorDetail : skillDetail),
    ),
    listInstallations: vi.fn(() => Promise.resolve(INSTALLATIONS)),
    listRegistries: vi.fn(() => Promise.resolve(REGISTRIES)),
    publishListing: vi.fn(() => Promise.resolve(skillDetail)),
    previewInstall: vi.fn(() =>
      Promise.resolve({
        registry_id: skill.registry_id,
        kind: "skill_profile" as const,
        slug: "python-pro",
        version: "1.2.0",
        verification: {
          status: "verified" as const,
          content_hash_ok: true,
          signature_ok: true,
        },
        resolved_config: { content_hash: `sha256:${"a".repeat(64)}` },
        warnings: [],
        requires_admin_followup: [],
        overrides_builtin: false,
        blocked: false,
        block_reason: null,
      }),
    ),
    installPackage: vi.fn(),
    updateInstallation: vi.fn(() =>
      Promise.resolve({
        installation_id: "inst-1",
        target_kind: "skill_profile",
        target_object_id: "obj-1",
        status: "installed" as const,
        version: "1.2.0",
        verification: { status: "verified" as const, content_hash_ok: true },
        warnings: [],
      }),
    ),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(<MarketplaceView client={client} />, { wrapper: Wrapper });
}

describe("MarketplaceView", () => {
  it("renders the catalog and auto-selects the first package's detail", async () => {
    renderView(makeClient());

    expect(
      await screen.findByTestId("package-card-python-pro"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("package-card-github")).toBeInTheDocument();
    expect(screen.getByText(/2\s*packages/i)).toBeInTheDocument();

    // Detail rail resolves the first package with its version provenance.
    expect(
      await screen.findByRole("heading", { level: 2, name: /python pro/i }),
    ).toBeInTheDocument();
    // Version history (and its yanked marker) confirms the detail resolved.
    expect(await screen.findByTestId("yanked-1.1.0")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: /versions/i })).toHaveTextContent(
      "v1.2.0",
    );
  });

  it("filters the catalog client-side as you type (instant search)", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("package-card-python-pro");

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "github" },
    });

    expect(screen.getByTestId("package-card-github")).toBeInTheDocument();
    expect(
      screen.queryByTestId("package-card-python-pro"),
    ).not.toBeInTheDocument();
    // No refetch — the catalog was filtered in memory.
    expect(client.listListings).toHaveBeenCalledTimes(1);
  });

  it("narrows by kind on the server", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("package-card-python-pro");

    fireEvent.click(screen.getByRole("button", { name: "MCP connectors" }));

    await waitFor(() =>
      expect(client.listListings).toHaveBeenCalledWith({ kind: "mcp_connector" }),
    );
  });

  it("shows the empty-search state when nothing matches", async () => {
    renderView(makeClient());
    await screen.findByTestId("package-card-python-pro");

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "nonexistent-zzz" },
    });

    expect(screen.getByTestId("empty-search")).toBeInTheDocument();
  });

  it("selects another package and loads its detail", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("package-card-github");

    fireEvent.click(screen.getByTestId("package-card-github"));

    expect(
      await screen.findByRole("heading", { level: 2, name: /github connector/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(client.getListing).toHaveBeenCalledWith("acme-registry", "github"),
    );
  });

  it("opens the install dialog and previews the package", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByRole("heading", { level: 2, name: /python pro/i });

    fireEvent.click(screen.getByTestId("install-package"));

    expect(await screen.findByTestId("install-dialog")).toBeInTheDocument();
    await waitFor(() => expect(client.previewInstall).toHaveBeenCalled());
  });

  it("lists installed packages and updates one with an available version", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("package-card-python-pro");

    fireEvent.click(screen.getByRole("tab", { name: /installed/i }));

    expect(
      await screen.findByTestId("installation-python-pro"),
    ).toBeInTheDocument();
    // The up-to-date row offers no update action.
    expect(
      screen.queryByTestId("update-github"),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("update-python-pro"));

    await waitFor(() =>
      expect(client.updateInstallation).toHaveBeenCalledWith("inst-1", "1.2.0"),
    );
  });

  it("shows the empty catalog state", async () => {
    const client = makeClient({ listListings: vi.fn(() => Promise.resolve([])) });
    renderView(client);
    expect(await screen.findByTestId("empty-catalog")).toBeInTheDocument();
  });

  it("shows a loading skeleton while the catalog is in flight", () => {
    const client = makeClient({
      listListings: vi.fn(() => new Promise<Listing[]>(() => {})),
    });
    renderView(client);
    expect(screen.getByTestId("catalog-skeleton")).toBeInTheDocument();
  });

  it("degrades gracefully when the catalog errors", async () => {
    const client = makeClient({
      listListings: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(client);
    expect(await screen.findByTestId("catalog-error")).toBeInTheDocument();
  });

  it("shows the empty + error states on the installed tab", async () => {
    const emptyClient = makeClient({
      listInstallations: vi.fn(() => Promise.resolve([])),
    });
    const { unmount } = renderView(emptyClient);
    await screen.findByTestId("package-card-python-pro");
    fireEvent.click(screen.getByRole("tab", { name: /installed/i }));
    expect(await screen.findByTestId("empty-installed")).toBeInTheDocument();
    unmount();

    const errorClient = makeClient({
      listInstallations: vi.fn(() => Promise.reject(new Error("offline"))),
    });
    renderView(errorClient);
    await screen.findByTestId("package-card-python-pro");
    fireEvent.click(screen.getByRole("tab", { name: /installed/i }));
    expect(await screen.findByTestId("installed-error")).toBeInTheDocument();
  });

  it("opens the publish dialog and publishes a new package", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByTestId("package-card-python-pro");

    fireEvent.click(screen.getByTestId("open-publish"));
    expect(await screen.findByTestId("publish-dialog")).toBeInTheDocument();
    await waitFor(() => expect(client.listRegistries).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Slug"), {
      target: { value: "self-authored" },
    });
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Self Authored" },
    });
    fireEvent.change(screen.getByLabelText("Summary"), {
      target: { value: "A workspace-authored skill profile." },
    });
    fireEvent.change(screen.getByLabelText("Artifact JSON"), {
      target: { value: '{"name": "self-authored"}' },
    });

    fireEvent.click(screen.getByTestId("confirm-publish"));

    await waitFor(() => expect(client.publishListing).toHaveBeenCalled());
    // Success closes the dialog and refetches the catalog.
    await waitFor(() =>
      expect(screen.queryByTestId("publish-dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(client.listListings).toHaveBeenCalledTimes(2));
  });
});
