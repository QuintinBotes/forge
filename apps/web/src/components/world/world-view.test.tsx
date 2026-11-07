import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  ConflictOut,
  EntityOut,
  EntityProfileOut,
  FactOut,
  ProposalOut,
} from "@/lib/api/types";

import { WorldView } from "./world-view";

let mockSearchParams = new URLSearchParams();
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => mockSearchParams,
}));

function entity(id: string, label: string, type = "person"): EntityOut {
  return {
    id,
    label,
    type_key: type,
    type_source: "asserted",
    described: false,
    described_in: null,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function fact(over: Partial<FactOut> = {}): FactOut {
  return {
    id: "f1",
    subject_id: "e1",
    phrase: "worked on",
    predicate_key: "worked_on",
    object_id: "e2",
    value: null,
    layer: "asserted",
    mood: null,
    qualifiers: {},
    confidence: 0.92,
    valid_from: "1843-01-01T00:00:00Z",
    valid_from_precision: "year",
    valid_to: null,
    valid_to_precision: "unknown",
    end_derived: false,
    end_after_period: false,
    source_kind: "agent",
    actor: "agent-1",
    proposal_id: null,
    supersedes_id: null,
    recorded_at: "2026-01-02T00:00:00Z",
    invalidated_at: null,
    invalidated_reason: null,
    evidence: [
      {
        id: "ev1",
        source_ref: "doc:memoir",
        chunk_index: 0,
        quote: "Ada translated the memoir",
        start_offset: 0,
        end_offset: 24,
        doc_time: null,
      },
    ],
    ...over,
  };
}

const ada = entity("e1", "Ada Lovelace");
const engine = entity("e2", "Analytical Engine", "machine");
const babbage = entity("e3", "Charles Babbage");

function profileFor(id: string): EntityProfileOut {
  if (id === "e1") {
    return {
      entity: ada,
      resolved_id: "e1",
      names: ["Ada Lovelace", "Augusta Ada King"],
      facts: [
        fact(),
        fact({
          id: "f2",
          phrase: "born in",
          predicate_key: "born_in",
          object_id: null,
          value: "London",
          valid_from: "1815-12-10T00:00:00Z",
          valid_from_precision: "day",
          evidence: [],
        }),
      ],
      object_of: [
        fact({
          id: "f3",
          subject_id: "e3",
          phrase: "collaborated with",
          predicate_key: "collaborated_with",
          object_id: "e1",
          evidence: [],
        }),
      ],
    };
  }
  if (id === "e2") {
    return {
      entity: engine,
      resolved_id: "e2",
      names: ["Analytical Engine"],
      facts: [],
      object_of: [fact()],
    };
  }
  if (id === "e3") {
    return {
      entity: babbage,
      resolved_id: "e3",
      names: ["Charles Babbage"],
      facts: [
        fact({
          id: "f3",
          subject_id: "e3",
          phrase: "collaborated with",
          predicate_key: "collaborated_with",
          object_id: "e1",
          evidence: [],
        }),
      ],
      object_of: [],
    };
  }
  throw new ApiError(404, "GET /world/entities failed with 404", {
    detail: "entity not found",
  });
}

const proposal: ProposalOut = {
  id: "p1",
  kind: "fact",
  payload: {
    subject: "Ada Lovelace",
    phrase: "wrote",
    predicate_key: "wrote",
    object: "Notes on the Analytical Engine",
    valid_from: "1843",
  },
  status: "proposed",
  proposer_kind: "agent",
  proposer: "agent-1",
  rationale: "Found in the memoir translation.",
  gate: { decision: "accept", confidence: 0.8 },
  decided_by: null,
  decided_at: null,
  decision_reason: null,
  fact_id: null,
  created_at: "2026-01-03T00:00:00Z",
};

const conflict: ConflictOut = {
  kind: "functional",
  predicate_key: "born_in",
  subject_id: "e1",
  object_id: null,
  fact_ids: ["f2", "f4"],
  detail: "Two different birthplaces are asserted.",
};

type MockClient = Record<string, ReturnType<typeof vi.fn>>;

function makeClient(overrides: MockClient = {}): ForgeApiClient {
  return {
    listWorldEntities: vi.fn(() =>
      Promise.resolve({ items: [ada, engine, babbage], limit: 50, offset: 0 }),
    ),
    getWorldEntity: vi.fn((id: string) => Promise.resolve(profileFor(id))),
    getWorldEntityChanges: vi.fn(() =>
      Promise.resolve({
        items: [
          {
            kind: "asserted",
            fact_id: "f1",
            occurred_at: "2026-01-02T00:00:00Z",
            reason: null,
          },
          {
            kind: "invalidated",
            fact_id: "f9",
            occurred_at: "2026-01-04T00:00:00Z",
            reason: "superseded by a corrected fact",
          },
        ],
      }),
    ),
    listWorldConflicts: vi.fn(() => Promise.resolve({ items: [conflict] })),
    listWorldProposals: vi.fn(() => Promise.resolve({ items: [proposal] })),
    decideWorldProposal: vi.fn(() =>
      Promise.resolve({
        proposal: { ...proposal, status: "accepted" },
        fact: null,
      }),
    ),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<WorldView client={client} />, { wrapper });
}

async function selectAda(client: ForgeApiClient) {
  const utils = renderView(client);
  fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
  await screen.findByRole("heading", { name: "Ada Lovelace" });
  return utils;
}

describe("WorldView", () => {
  it("search → select renders profile facts, evidence and neighbour nodes", async () => {
    const client = makeClient();
    const { container } = await selectAda(client);

    // Facts with precision-aware valid time and provenance.
    expect(screen.getAllByText("worked on").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1843 – open").length).toBeGreaterThan(0);
    expect(screen.getByText("1815-12-10 – open")).toBeInTheDocument();
    expect(screen.getByText("London")).toBeInTheDocument();
    expect(screen.getAllByText("92%").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/agent · agent-1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Augusta Ada King/)).toBeInTheDocument();

    // Neighbour nodes (outgoing object + incoming subject) with labels.
    expect(
      container.querySelector('g[aria-label="Focus Analytical Engine"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('g[aria-label="Focus Charles Babbage"]'),
    ).not.toBeNull();
    expect(
      screen.getByRole("heading", { name: "Connections" }),
    ).toBeInTheDocument();

    // Evidence expands to its quote and source.
    fireEvent.click(
      screen.getByRole("button", { name: /Evidence for worked on/ }),
    );
    expect(
      await screen.findByText(/Ada translated the memoir/),
    ).toBeInTheDocument();
    expect(screen.getByText("doc:memoir")).toBeInTheDocument();

    // Change timeline.
    expect(
      await screen.findByText("superseded by a corrected fact"),
    ).toBeInTheDocument();

    expect(client.getWorldEntity).toHaveBeenCalledWith(
      "e1",
      expect.objectContaining({}),
    );
  });

  it("clicking a neighbour node refocuses on it and updates the URL", async () => {
    mockReplace.mockClear();
    const client = makeClient();
    const { container } = await selectAda(client);

    // Wait until the neighbour's label has resolved before clicking it.
    await waitFor(() =>
      expect(
        container.querySelector('g[aria-label="Focus Analytical Engine"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(
      container.querySelector(
        'g[aria-label="Focus Analytical Engine"]',
      ) as Element,
    );

    expect(
      await screen.findByRole("heading", { name: "Analytical Engine" }),
    ).toBeInTheDocument();
    expect(client.getWorldEntity).toHaveBeenCalledWith(
      "e2",
      expect.objectContaining({}),
    );
    expect(mockReplace).toHaveBeenCalledWith("/world?entity=e2", {
      scroll: false,
    });
  });

  it("Enter on a focused neighbour node refocuses on it", async () => {
    const client = makeClient();
    const { container } = await selectAda(client);

    await waitFor(() =>
      expect(
        container.querySelector('g[aria-label="Focus Charles Babbage"]'),
      ).not.toBeNull(),
    );
    fireEvent.keyDown(
      container.querySelector(
        'g[aria-label="Focus Charles Babbage"]',
      ) as Element,
      { key: "Enter" },
    );

    expect(
      await screen.findByRole("heading", { name: "Charles Babbage" }),
    ).toBeInTheDocument();
  });

  it("focuses the entity named by ?entity= on load", async () => {
    mockSearchParams = new URLSearchParams("entity=e1");
    try {
      const client = makeClient();
      renderView(client);
      expect(
        await screen.findByRole("heading", { name: "Ada Lovelace" }),
      ).toBeInTheDocument();
      expect(client.getWorldEntity).toHaveBeenCalledWith(
        "e1",
        expect.objectContaining({}),
      );
    } finally {
      mockSearchParams = new URLSearchParams();
    }
  });

  it("passes ISO at and as_of from the time-travel inputs to search and profile", async () => {
    const client = makeClient();
    const listWorldEntities = client.listWorldEntities as ReturnType<
      typeof vi.fn
    >;
    const listWorldConflicts = client.listWorldConflicts as ReturnType<
      typeof vi.fn
    >;
    renderView(client);

    fireEvent.change(screen.getByLabelText("World time (at)"), {
      target: { value: "1843-06-01T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Known as of (as_of)"), {
      target: { value: "2020-01-02T03:04" },
    });

    const at = new Date("1843-06-01T00:00").toISOString();
    const asOf = new Date("2020-01-02T03:04").toISOString();
    await waitFor(() =>
      expect(listWorldEntities).toHaveBeenCalledWith(
        expect.objectContaining({ at, as_of: asOf }),
      ),
    );
    // Conflicts receive `at` only.
    expect(listWorldConflicts).toHaveBeenCalledWith({ at });

    fireEvent.click(
      await screen.findByRole("button", { name: /Ada Lovelace/ }),
    );
    await waitFor(() =>
      expect(client.getWorldEntity).toHaveBeenCalledWith("e1", {
        at,
        as_of: asOf,
      }),
    );

    // "Now" clears both clocks.
    fireEvent.click(screen.getByRole("button", { name: "Now" }));
    await waitFor(() =>
      expect(listWorldEntities).toHaveBeenLastCalledWith(
        expect.objectContaining({ at: undefined, as_of: undefined }),
      ),
    );
  });

  it("debounces search input into the q parameter", async () => {
    const client = makeClient();
    const listWorldEntities = client.listWorldEntities as ReturnType<
      typeof vi.fn
    >;
    renderView(client);

    fireEvent.change(screen.getByLabelText("Search entities"), {
      target: { value: "ada" },
    });

    await waitFor(
      () =>
        expect(listWorldEntities).toHaveBeenCalledWith(
          expect.objectContaining({ q: "ada" }),
        ),
      { timeout: 2000 },
    );
  });

  it("shows conflicts and focuses a conflict's subject in Explore", async () => {
    const client = makeClient();
    renderView(client);

    fireEvent.click(screen.getByRole("tab", { name: "Conflicts" }));
    expect(
      await screen.findByText("Two different birthplaces are asserted."),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /Focus subject.*Ada Lovelace/ }),
    );

    expect(
      await screen.findByRole("heading", { name: "Ada Lovelace" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Explore" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("Accept posts {accept: true}", async () => {
    const client = makeClient();
    renderView(client);

    fireEvent.click(screen.getByRole("tab", { name: "Proposals" }));
    expect(
      await screen.findByText("Notes on the Analytical Engine"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Gate: accept/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() =>
      expect(client.decideWorldProposal).toHaveBeenCalledWith("p1", {
        accept: true,
      }),
    );
  });

  it("shows the API detail when a decision is refused with 403", async () => {
    const client = makeClient({
      decideWorldProposal: vi.fn(() =>
        Promise.reject(
          new ApiError(403, "POST /world/proposals/p1/decision failed", {
            detail: "a proposer cannot decide their own proposal",
          }),
        ),
      ),
    });
    renderView(client);

    fireEvent.click(screen.getByRole("tab", { name: "Proposals" }));
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));

    expect(
      await screen.findByText("a proposer cannot decide their own proposal"),
    ).toBeInTheDocument();
  });

  it("renders the workspace empty state when there are no entities", async () => {
    const client = makeClient({
      listWorldEntities: vi.fn(() =>
        Promise.resolve({ items: [], limit: 50, offset: 0 }),
      ),
    });
    renderView(client);

    expect(await screen.findByText("No entities yet")).toBeInTheDocument();
    expect(screen.getByText(/world_propose/)).toBeInTheDocument();
    expect(screen.getByText(/Proposals tab/)).toBeInTheDocument();
    expect(screen.getByText(/ingest/)).toBeInTheDocument();
  });
});
