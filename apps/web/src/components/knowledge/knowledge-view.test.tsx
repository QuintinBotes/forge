import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import { useCurrentUser } from "@/lib/api/hooks";
import type {
  KnowledgeDocumentResponse,
  KnowledgePassage,
  KnowledgeSearchResponse,
  KnowledgeSourceSummary,
} from "@/lib/api/types";

import { KnowledgeView } from "./knowledge-view";

function source(over: Partial<KnowledgeSourceSummary> = {}): KnowledgeSourceSummary {
  return {
    id: "s1",
    name: "forge repo",
    kind: "repo",
    uri: "github.com/o/forge",
    tracker: null,
    syncable: false,
    sync_mode: "full",
    last_synced_at: "2026-11-01T00:00:00Z",
    freshness_sla_minutes: 60,
    stale: true,
    chunk_count: 340,
    embedded_chunk_count: 12,
    newest_chunk_at: "2026-11-01T00:00:00Z",
    ...over,
  };
}

const tracker = source({
  id: "s2",
  name: "forge tracker",
  kind: "url",
  uri: "https://github.com/o/forge",
  tracker: "github",
  syncable: true,
  last_synced_at: null,
  stale: false,
  freshness_sla_minutes: null,
  chunk_count: 0,
  embedded_chunk_count: 0,
  newest_chunk_at: null,
});

function passage(over: Partial<KnowledgePassage> = {}): KnowledgePassage {
  return {
    id: "c1",
    source_id: "s1",
    source_uri: "github.com/o/forge",
    path: "docs/rotation.md",
    content: "Tokens rotate daily.",
    chunk_type: "markdown",
    score: 0.0325,
    rerank_score: null,
    metadata: { source_kind: "repo" },
    source: "repo:github.com/o/forge:docs/rotation.md",
    ...over,
  };
}

const hostile =
  '<script>window.pwned = true</script> **bold** [click](javascript:alert(1)) <img src=x onerror="alert(1)">';

const searchResponse: KnowledgeSearchResponse = {
  results: [
    passage(),
    passage({
      id: "c2",
      source_id: "s2",
      source_uri: "https://github.com/o/forge",
      path: "issues/9",
      content: hostile,
      metadata: { source_kind: "url" },
      source: "url:https://github.com/o/forge:issues/9",
    }),
    passage({
      id: "c3",
      path: `commits/${"c".repeat(40)}`,
      content: "Tune rotation policy",
      source: `repo:github.com/o/forge:commits/${"c".repeat(40)}`,
    }),
  ],
  legs: ["bm25"],
  vector_leg: "unconfigured",
  rerank: "off",
};

const documentResponse: KnowledgeDocumentResponse = {
  source_id: "s1",
  source: "github.com/o/forge",
  path: "docs/rotation.md",
  passages: [
    passage({ id: "d1", content: "docs/rotation.md › Rotation\n# Rotation\nTokens rotate daily." }),
  ],
  tokens: 12,
  total_chunks: 3,
  truncated: true,
};

function makeClient(over: Record<string, unknown> = {}): ForgeApiClient {
  return {
    me: vi.fn(() => Promise.resolve({ user_id: "u", workspace_id: "w", role: "member" })),
    listKnowledgeSources: vi.fn(() => Promise.resolve([source(), tracker])),
    syncKnowledgeSource: vi.fn(() => Promise.resolve({ status: "enqueued", source_id: "s1" })),
    searchKnowledge: vi.fn(() => Promise.resolve(searchResponse)),
    getKnowledgeDocument: vi.fn(() => Promise.resolve(documentResponse)),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient, extra?: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <>
      <KnowledgeView client={client} />
      {extra}
    </>,
    { wrapper },
  );
}

/** Shows the role once the shared `/auth/me` query resolves: the table re-renders with it. */
function RoleProbe({ client }: { client: ForgeApiClient }) {
  return <span data-testid="role-probe">{useCurrentUser(client).data?.role ?? ""}</span>;
}

const asAdmin = () =>
  vi.fn(() => Promise.resolve({ user_id: "u", workspace_id: "w", role: "admin" }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function searchFor(query: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Search knowledge"), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
  await screen.findByTestId("knowledge-legs");
  return user;
}

describe("KnowledgeView", () => {
  it("lists sources with embedding coverage and a stale badge", async () => {
    const client = makeClient();
    renderView(client);

    const row = (await screen.findByText("forge repo")).closest("tr") as HTMLElement;
    expect(within(row).getByText("12 of 340 embedded")).toBeInTheDocument();
    expect(
      within(row).getByRole("meter", { name: "Embedding coverage for forge repo" }),
    ).toHaveAttribute("aria-valuenow", "4");
    expect(within(row).getByText("Stale")).toBeInTheDocument();

    const trackerRow = screen.getByText("forge tracker").closest("tr") as HTMLElement;
    expect(within(trackerRow).queryByText("Stale")).toBeNull();
    expect(within(trackerRow).getByText(/github tracker/)).toBeInTheDocument();
    expect(within(trackerRow).getByText("Never")).toBeInTheDocument();
    expect(within(trackerRow).getByText("0 of 0 embedded")).toBeInTheDocument();
  });

  it("never shows Sync now to a member once /auth/me has resolved", async () => {
    const client = makeClient();
    renderView(client, <RoleProbe client={client} />);
    await screen.findByText("forge tracker"); // a syncable row: an admin would see the button
    await waitFor(() => expect(screen.getByTestId("role-probe")).toHaveTextContent("member"));
    expect(screen.queryByRole("button", { name: /Sync/ })).toBeNull();
  });

  const jira = { ...tracker, id: "s4", name: "jira tracker", tracker: "jira", syncable: false };

  it("offers Sync now to an admin only on syncable rows and refreshes the row", async () => {
    const listKnowledgeSources = vi
      .fn()
      .mockResolvedValueOnce([source(), tracker, jira])
      .mockResolvedValue([
        source(),
        { ...tracker, last_synced_at: "2026-11-02T00:00:00Z", chunk_count: 5, embedded_chunk_count: 5 },
        jira,
      ]);
    const client = makeClient({ me: asAdmin(), listKnowledgeSources });
    renderView(client);

    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Sync now forge tracker" });
    const repoRow = screen.getByText("forge repo").closest("tr") as HTMLElement;
    expect(within(repoRow).queryByRole("button")).toBeNull(); // the endpoint can't sync it by id
    const jiraRow = screen.getByText("jira tracker").closest("tr") as HTMLElement;
    expect(within(jiraRow).queryByRole("button")).toBeNull(); // a url source, but not syncable
    await user.click(button);

    expect(client.syncKnowledgeSource).toHaveBeenCalledWith("s2");
    expect(await screen.findByText("5 of 5 embedded")).toBeInTheDocument();
    expect(listKnowledgeSources).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Sync requested")).toBeInTheDocument();
  });

  it("keeps each row's sync state its own, busy and named while pending", async () => {
    const pending = deferred<unknown>();
    const second = { ...tracker, id: "s3", name: "docs tracker" };
    const client = makeClient({
      me: asAdmin(),
      listKnowledgeSources: vi.fn(() => Promise.resolve([source(), tracker, second])),
      syncKnowledgeSource: vi.fn(() => pending.promise),
    });
    renderView(client);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sync now forge tracker" }));

    const busy = screen.getByRole("button", { name: "Syncing… forge tracker" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toBeDisabled();
    const other = screen.getByRole("button", { name: "Sync now docs tracker" });
    expect(other).toHaveAttribute("aria-busy", "false");
    expect(other).toBeEnabled();

    await act(async () => pending.resolve({ status: "enqueued", source_id: "s2" }));
    const trackerRow = busy.closest("tr") as HTMLElement;
    const docsRow = other.closest("tr") as HTMLElement;
    expect(await within(trackerRow).findByText("Sync requested")).toBeInTheDocument();
    expect(within(docsRow).queryByText("Sync requested")).toBeNull();
  });

  it("shows a sync refusal on its row", async () => {
    const client = makeClient({
      me: asAdmin(),
      syncKnowledgeSource: vi.fn(() =>
        Promise.reject(
          new ApiError(404, "POST /knowledge/sync failed with 404", {
            detail: "knowledge source not found",
          }),
        ),
      ),
    });
    renderView(client);
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Sync now forge tracker" });
    await user.click(button);
    const trackerRow = button.closest("tr") as HTMLElement;
    expect(await within(trackerRow).findByRole("alert")).toHaveTextContent(
      "knowledge source not found",
    );
  });

  it("states which legs ran, unconfigured included, and filters by kind", async () => {
    const client = makeClient();
    renderView(client);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Source kind"), "repo");
    await searchFor("rotation");

    const legs = screen.getByTestId("knowledge-legs");
    expect(legs).toHaveTextContent("Legs that ran: bm25");
    expect(legs).toHaveTextContent("vector leg: unconfigured (no embedder: keyword search only)");
    expect(legs).toHaveTextContent("reranker: off");
    expect(client.searchKnowledge).toHaveBeenCalledWith({
      query: "rotation",
      scope: { source_types: ["repo"] },
    });
  });

  it("states a reranker fallback plainly", async () => {
    renderView(
      makeClient({
        searchKnowledge: vi.fn(() =>
          Promise.resolve({
            ...searchResponse,
            legs: ["bm25", "vector"],
            vector_leg: "configured",
            rerank: "fallback",
          }),
        ),
      }),
    );
    await searchFor("rotation");
    expect(screen.getByTestId("knowledge-legs")).toHaveTextContent(
      "Legs that ran: bm25 + vector · vector leg: configured · reranker: fallback (the reranker failed",
    );
  });

  it("renders hit text literally and labels tracker and commit content as external", async () => {
    const { container } = renderView(makeClient());
    await screen.findByText("forge repo");
    await searchFor("rotation");

    const results = screen.getByRole("list", { name: "Search results" });
    const items = within(results).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(within(items[0]).getByText("forge repo")).toBeInTheDocument();
    expect(within(items[0]).getByText("markdown")).toBeInTheDocument();
    expect(within(items[0]).getByText("score 0.033")).toBeInTheDocument();
    expect(within(items[0]).queryByText(/External content/)).toBeNull();

    expect(within(items[1]).getByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("script, img, strong, a[href^='javascript']")).toBeNull();
    expect((window as unknown as { pwned?: boolean }).pwned).toBeUndefined();
    expect(within(items[1]).getByText("forge tracker")).toBeInTheDocument();
    expect(within(items[1]).getByText("External content (tracker item)")).toBeInTheDocument();
    expect(within(items[2]).getByText("External content (commit message)")).toBeInTheDocument();
  });

  it("labels MCP passages as external content", async () => {
    renderView(
      makeClient({
        searchKnowledge: vi.fn(() =>
          Promise.resolve({
            ...searchResponse,
            results: [
              passage({
                id: "m1",
                source_id: "s9",
                source_uri: "mcp://notion",
                path: "mcp://notion/page-1",
                metadata: { source_kind: "mcp" },
                source: "mcp:mcp://notion:mcp://notion/page-1",
              }),
            ],
          }),
        ),
      }),
    );
    await searchFor("rotation");
    const [item] = within(screen.getByRole("list", { name: "Search results" })).getAllByRole(
      "listitem",
    );
    expect(within(item).getByText("External content (MCP resource)")).toBeInTheDocument();
  });

  it("renders document text literally (no HTML from the document view)", async () => {
    const client = makeClient({
      getKnowledgeDocument: vi.fn(() =>
        Promise.resolve({
          ...documentResponse,
          passages: [passage({ id: "d9", content: hostile })],
        }),
      ),
    });
    renderView(client);
    const user = await searchFor("rotation");
    await user.click(screen.getByRole("button", { name: "docs/rotation.md" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(hostile)).toBeInTheDocument();
    expect(dialog.querySelector("script, img, strong, a")).toBeNull();
    expect((window as unknown as { pwned?: boolean }).pwned).toBeUndefined();
  });

  it("opens the whole document, says it was truncated, and returns focus on close", async () => {
    const client = makeClient();
    renderView(client);
    await screen.findByText("forge repo");
    const user = await searchFor("rotation");

    const hit = screen.getByRole("button", { name: "docs/rotation.md" });
    await user.click(hit);

    const dialog = await screen.findByRole("dialog");
    expect(client.getKnowledgeDocument).toHaveBeenCalledWith("s1", "docs/rotation.md");
    expect(
      await within(dialog).findByText("Truncated: showing 1 of 3 chunks (12 tokens)."),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/# Rotation/)).toBeInTheDocument();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(hit).toHaveFocus());
  });

  it("shows empty states for no sources and no matches", async () => {
    renderView(
      makeClient({
        listKnowledgeSources: vi.fn(() => Promise.resolve([])),
        searchKnowledge: vi.fn(() => Promise.resolve({ ...searchResponse, results: [] })),
      }),
    );
    expect(await screen.findByText("No knowledge sources yet")).toBeInTheDocument();
    await searchFor("nothing");
    expect(screen.getByText("No passages match")).toBeInTheDocument();
  });

  it("shows error states for sources, search and the document", async () => {
    const refused = (status: number, detail: string) => () =>
      Promise.reject(new ApiError(status, `failed with ${status}`, { detail }));
    const client = makeClient({
      listKnowledgeSources: vi.fn(refused(500, "database unavailable")),
      searchKnowledge: vi.fn(refused(503, "vector search is unconfigured")),
    });
    renderView(client);
    expect(await screen.findByText("Sources didn't load")).toBeInTheDocument();
    expect(screen.getByText("database unavailable")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Search knowledge"), "rotation");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Search failed")).toBeInTheDocument();
    expect(screen.getByText("vector search is unconfigured")).toBeInTheDocument();
  });

  it("shows a document that fails to load", async () => {
    renderView(
      makeClient({
        getKnowledgeDocument: vi.fn(() =>
          Promise.reject(new ApiError(404, "failed with 404", { detail: "document not found" })),
        ),
      }),
    );
    const user = await searchFor("rotation");
    await user.click(screen.getByRole("button", { name: "docs/rotation.md" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Document didn't load")).toBeInTheDocument();
    expect(within(dialog).getByText("document not found")).toBeInTheDocument();
  });
});
