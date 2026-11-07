import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ForgeApiClient } from "./client";
import type {
  EntityListOut,
  EntityOut,
  EntityProfileOut,
  ProposalListOut,
  ProposalOut,
} from "./types";
import {
  useDecideWorldProposal,
  useWorldConflicts,
  useWorldEntities,
  useWorldEntityProfile,
  useWorldNeighbourEntities,
  useWorldProposals,
} from "./world";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function entity(id: string, label: string): EntityOut {
  return {
    id,
    label,
    type_key: "person",
    type_source: "asserted",
    described: false,
    described_in: null,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function profile(id: string, label: string): EntityProfileOut {
  return {
    entity: entity(id, label),
    resolved_id: id,
    names: [label],
    facts: [],
    object_of: [],
  };
}

const AT = "1843-06-01T00:00:00.000Z";
const AS_OF = "2020-01-02T03:04:00.000Z";

describe("useWorldEntities", () => {
  it("passes the search text and both clocks to the client", async () => {
    const page: EntityListOut = {
      items: [entity("e1", "Ada Lovelace")],
      limit: 50,
      offset: 0,
    };
    const listWorldEntities = vi.fn(() => Promise.resolve(page));
    const client = { listWorldEntities } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () =>
        useWorldEntities({ q: "ada", at: AT, as_of: AS_OF, limit: 50 }, client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(listWorldEntities).toHaveBeenCalledWith({
      q: "ada",
      at: AT,
      as_of: AS_OF,
      limit: 50,
    });
    expect(result.current.data?.items[0].label).toBe("Ada Lovelace");
  });
});

describe("useWorldEntityProfile", () => {
  it("passes at/as_of to the profile call", async () => {
    const getWorldEntity = vi.fn((id: string) =>
      Promise.resolve(profile(id, "Ada Lovelace")),
    );
    const client = { getWorldEntity } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => useWorldEntityProfile("e1", { at: AT, as_of: AS_OF }, client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getWorldEntity).toHaveBeenCalledWith("e1", {
      at: AT,
      as_of: AS_OF,
    });
  });

  it("does not fetch until an entity is focused", async () => {
    const getWorldEntity = vi.fn((id: string) =>
      Promise.resolve(profile(id, "Ada Lovelace")),
    );
    const client = { getWorldEntity } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => useWorldEntityProfile(null, undefined, client),
      { wrapper: makeWrapper(newClient()) },
    );

    expect(result.current.fetchStatus).toBe("idle");
    expect(getWorldEntity).not.toHaveBeenCalled();
  });
});

describe("useWorldNeighbourEntities", () => {
  it("resolves one profile per neighbour id, keyed by id", async () => {
    const getWorldEntity = vi.fn((id: string) =>
      Promise.resolve(profile(id, `Label ${id}`)),
    );
    const client = { getWorldEntity } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => useWorldNeighbourEntities(["e2", "e3"], { at: AT }, client),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.entities.size).toBe(2));
    expect(result.current.entities.get("e2")?.label).toBe("Label e2");
    expect(getWorldEntity).toHaveBeenCalledWith("e3", { at: AT });
  });
});

describe("useWorldConflicts", () => {
  it("passes at only (the endpoint does not accept as_of)", async () => {
    const listWorldConflicts = vi.fn(() => Promise.resolve({ items: [] }));
    const client = { listWorldConflicts } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useWorldConflicts(AT, client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(listWorldConflicts).toHaveBeenCalledWith({ at: AT });
  });
});

describe("useDecideWorldProposal", () => {
  it("invalidates proposals, profiles and search on success", async () => {
    const proposal: ProposalOut = {
      id: "p1",
      kind: "fact",
      payload: { subject: "Ada Lovelace", phrase: "wrote" },
      status: "proposed",
      proposer_kind: "agent",
      proposer: "agent-1",
      rationale: "",
      gate: {},
      decided_by: null,
      decided_at: null,
      decision_reason: null,
      fact_id: null,
      created_at: "2026-01-01T00:00:00Z",
    };
    const listWorldProposals = vi.fn(() =>
      Promise.resolve({ items: [proposal] } as ProposalListOut),
    );
    const listWorldEntities = vi.fn(() =>
      Promise.resolve({
        items: [entity("e1", "Ada Lovelace")],
        limit: 50,
        offset: 0,
      } as EntityListOut),
    );
    const getWorldEntity = vi.fn((id: string) =>
      Promise.resolve(profile(id, "Ada Lovelace")),
    );
    const decideWorldProposal = vi.fn(() =>
      Promise.resolve({
        proposal: { ...proposal, status: "accepted" },
        fact: null,
      }),
    );
    const client = {
      listWorldProposals,
      listWorldEntities,
      getWorldEntity,
      decideWorldProposal,
    } as unknown as ForgeApiClient;

    const { result } = renderHook(
      () => ({
        proposals: useWorldProposals({ status: "proposed" }, client),
        entities: useWorldEntities({}, client),
        profile: useWorldEntityProfile("e1", undefined, client),
        decide: useDecideWorldProposal(client),
      }),
      { wrapper: makeWrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.proposals.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.entities.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.profile.isSuccess).toBe(true));
    expect(listWorldProposals).toHaveBeenCalledTimes(1);
    expect(listWorldEntities).toHaveBeenCalledTimes(1);
    expect(getWorldEntity).toHaveBeenCalledTimes(1);

    result.current.decide.mutate({ proposalId: "p1", accept: true });

    await waitFor(() => expect(result.current.decide.isSuccess).toBe(true));
    expect(decideWorldProposal).toHaveBeenCalledWith("p1", { accept: true });
    // Each active query refetches after invalidation.
    await waitFor(() => expect(listWorldProposals).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(listWorldEntities).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getWorldEntity).toHaveBeenCalledTimes(2));
  });

  it("sends the reason when one is given", async () => {
    const decideWorldProposal = vi.fn(() =>
      Promise.resolve({ proposal: {} as ProposalOut, fact: null }),
    );
    const client = { decideWorldProposal } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useDecideWorldProposal(client), {
      wrapper: makeWrapper(newClient()),
    });

    result.current.mutate({
      proposalId: "p9",
      accept: false,
      reason: "not supported by the source",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(decideWorldProposal).toHaveBeenCalledWith("p9", {
      accept: false,
      reason: "not supported by the source",
    });
  });
});

// --- Client: each world method's path, method, query and body ------------- //

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeFetchClient(response: Response) {
  const fetchImpl = vi.fn(() => Promise.resolve(response));
  const client = new ForgeApiClient({
    baseUrl: "http://api.test",
    fetch: fetchImpl as unknown as typeof fetch,
  });
  return { client, fetchImpl };
}

function calledUrl(fetchImpl: ReturnType<typeof vi.fn>): URL {
  return new URL(fetchImpl.mock.calls[0][0] as string);
}

function calledInit(fetchImpl: ReturnType<typeof vi.fn>): RequestInit {
  return fetchImpl.mock.calls[0][1] as RequestInit;
}

describe("ForgeApiClient — world model", () => {
  it("searches entities with q, type and both clocks", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, { items: [], limit: 10, offset: 5 }),
    );

    await client.listWorldEntities({
      q: "ada",
      type: "person",
      at: AT,
      as_of: AS_OF,
      limit: 10,
      offset: 5,
    });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/entities");
    expect(url.searchParams.get("q")).toBe("ada");
    expect(url.searchParams.get("type")).toBe("person");
    expect(url.searchParams.get("at")).toBe(AT);
    expect(url.searchParams.get("as_of")).toBe(AS_OF);
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("offset")).toBe("5");
    expect(calledInit(fetchImpl).method).toBe("GET");
  });

  it("fetches a profile with the clocks and an encoded id", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, profile("e1", "Ada Lovelace")),
    );

    await client.getWorldEntity("e 1", { at: AT, as_of: AS_OF });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/entities/e%201");
    expect(url.searchParams.get("at")).toBe(AT);
    expect(url.searchParams.get("as_of")).toBe(AS_OF);
    expect(calledInit(fetchImpl).method).toBe("GET");
  });

  it("fetches entity changes with since/until", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, { items: [] }),
    );

    await client.getWorldEntityChanges("e1", { since: AT, until: AS_OF });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/entities/e1/changes");
    expect(url.searchParams.get("since")).toBe(AT);
    expect(url.searchParams.get("until")).toBe(AS_OF);
    expect(calledInit(fetchImpl).method).toBe("GET");
  });

  it("lists conflicts with at", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, { items: [] }),
    );

    await client.listWorldConflicts({ at: AT });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/conflicts");
    expect(url.searchParams.get("at")).toBe(AT);
    expect(calledInit(fetchImpl).method).toBe("GET");
  });

  it("lists proposals by status with pagination", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, { items: [] }),
    );

    await client.listWorldProposals({
      status: "proposed",
      limit: 20,
      offset: 0,
    });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/proposals");
    expect(url.searchParams.get("status")).toBe("proposed");
    expect(url.searchParams.get("limit")).toBe("20");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(calledInit(fetchImpl).method).toBe("GET");
  });

  it("posts a proposal decision with the accept body", async () => {
    const { client, fetchImpl } = makeFetchClient(
      jsonResponse(200, { proposal: {}, fact: null }),
    );

    await client.decideWorldProposal("p1", { accept: true, reason: "sourced" });

    const url = calledUrl(fetchImpl);
    expect(url.pathname).toBe("/world/proposals/p1/decision");
    const init = calledInit(fetchImpl);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      accept: true,
      reason: "sourced",
    });
  });
});
