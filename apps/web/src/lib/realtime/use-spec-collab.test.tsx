import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import type { AwarenessLike, CollabProvider, ProviderFactoryArgs } from "./use-spec-collab";
import { SPEC_COLLAB_DOC_KEYS, cursorColorVar, useSpecCollab } from "./use-spec-collab";

/** A controllable fake `Awareness` (no `WebSocket`/network) for jsdom. */
function makeFakeAwareness(clientID: number) {
  const states = new Map<number, Record<string, unknown>>();
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((l) => l());
  const awareness: AwarenessLike & {
    setRemote: (id: number, state: Record<string, unknown>) => void;
  } = {
    clientID,
    getLocalState: () => states.get(clientID) ?? null,
    setLocalState(state) {
      if (state === null) states.delete(clientID);
      else states.set(clientID, state);
      emit();
    },
    setLocalStateField(field, value) {
      const current = states.get(clientID) ?? {};
      states.set(clientID, { ...current, [field]: value });
      emit();
    },
    getStates: () => states,
    on: (_event, cb) => listeners.add(cb),
    off: (_event, cb) => listeners.delete(cb),
    setRemote(id, state) {
      states.set(id, state);
      emit();
    },
  };
  return awareness;
}

/** A controllable fake `WebsocketProvider` bound to a real `Y.Doc`. */
function makeFakeProvider(clientID = 1) {
  const awareness = makeFakeAwareness(clientID);
  const statusCbs = new Set<(p: unknown) => void>();
  const syncCbs = new Set<(p: unknown) => void>();
  const provider: CollabProvider & {
    emitStatus: (status: string) => void;
    emitSync: (synced: boolean) => void;
    awareness: ReturnType<typeof makeFakeAwareness>;
    destroy: ReturnType<typeof vi.fn<() => void>>;
    disconnect: ReturnType<typeof vi.fn<() => void>>;
  } = {
    awareness,
    on(event, cb) {
      (event === "status" ? statusCbs : syncCbs).add(cb);
    },
    off(event, cb) {
      (event === "status" ? statusCbs : syncCbs).delete(cb);
    },
    disconnect: vi.fn<() => void>(),
    destroy: vi.fn<() => void>(),
    emitStatus: (status) => statusCbs.forEach((cb) => cb({ status })),
    emitSync: (synced) => syncCbs.forEach((cb) => cb(synced)),
  };
  return provider;
}

/** Harness: a real doc + fake provider, injected via the hook's factories. */
function makeHarness(clientID = 1) {
  const doc = new Y.Doc();
  const provider = makeFakeProvider(clientID);
  const providerFactory = vi.fn((args: ProviderFactoryArgs) => {
    // The default factory would open a real socket; assert wiring instead.
    void args;
    return provider;
  });
  return {
    doc,
    provider,
    providerFactory,
    docFactory: () => doc,
  };
}

describe("useSpecCollab", () => {
  it("seeds the shared text and propagates local edits into the Y.Text", () => {
    const h = makeHarness();
    const { result } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "markdown",
        seedText: "hello",
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );

    // Seeded once the initial state syncs from the server. Bound under the
    // SERVER's doc key (`spec.md`), not the raw field name — this is the
    // cross-stack CRDT contract with `spec_room.py`.
    act(() => h.provider.emitSync(true));
    expect(result.current.text).toBe("hello");
    expect(h.doc.getText("spec.md").toString()).toBe("hello");
    // And nothing lands under the raw field name (would never merge server-side).
    expect(h.doc.getText("markdown").toString()).toBe("");

    // A controlled-input change becomes a CRDT delta on the shared text.
    act(() => result.current.setText("hello world"));
    expect(h.doc.getText("spec.md").toString()).toBe("hello world");
    expect(result.current.text).toBe("hello world");
  });

  it("binds each surface to the server's Y.Doc key (cross-stack contract)", () => {
    // The pycrdt room keys its shared texts as `spec.md` / `manifest.yaml` and
    // materialises them through FileSpecEngine; the client must select the same
    // keys or edits never converge. Guard the mapping explicitly.
    expect(SPEC_COLLAB_DOC_KEYS).toEqual({
      markdown: "spec.md",
      yaml: "manifest.yaml",
    });

    const h = makeHarness();
    const { result } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "yaml",
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );
    act(() => h.provider.emitSync(true));

    // A peer edits the server-keyed `manifest.yaml` text → the client renders it.
    const remote = new Y.Doc();
    remote.getText("manifest.yaml").insert(0, "version: 1");
    act(() => Y.applyUpdate(h.doc, Y.encodeStateAsUpdate(remote)));
    expect(result.current.text).toBe("version: 1");
    expect(h.doc.getText("yaml").toString()).toBe("");
  });

  it("renders remote updates applied to the shared doc", () => {
    const h = makeHarness();
    const { result } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "markdown",
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );
    act(() => h.provider.emitSync(true));
    expect(result.current.text).toBe("");

    // Simulate a peer's edit arriving over the wire (server-keyed `spec.md`).
    const remote = new Y.Doc();
    remote.getText("spec.md").insert(0, "from a teammate");
    act(() => Y.applyUpdate(h.doc, Y.encodeStateAsUpdate(remote)));

    expect(result.current.text).toBe("from a teammate");
  });

  it("tracks connection status and remote presence with token colours", () => {
    const h = makeHarness(1);
    const { result } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "markdown",
        user: { name: "Ada" },
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );

    expect(result.current.connected).toBe(false);
    act(() => h.provider.emitStatus("connected"));
    expect(result.current.connected).toBe(true);

    // The local editor is present with its own name.
    expect(result.current.peers.some((p) => p.isSelf && p.name === "Ada")).toBe(true);

    // A remote editor joins → presence updates with a design-token colour.
    act(() =>
      h.provider.awareness.setRemote(42, {
        user: { name: "Grace" },
        cursor: { anchor: 2, head: 5 },
      }),
    );
    const grace = result.current.peers.find((p) => p.clientId === 42);
    expect(grace).toBeDefined();
    expect(grace?.name).toBe("Grace");
    expect(grace?.isSelf).toBe(false);
    expect(grace?.cursor).toEqual({ anchor: 2, head: 5 });
    expect(grace?.colorVar).toBe(cursorColorVar(42));
    expect(grace?.colorVar).toMatch(/^hsl\(var\(--chart-[1-6]\)\)$/);
  });

  it("publishes the local selection over awareness", () => {
    const h = makeHarness(7);
    const { result } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "yaml",
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );

    act(() => result.current.setSelection(3, 9));
    const self = h.provider.awareness.getStates().get(7);
    expect(self?.cursor).toEqual({ anchor: 3, head: 9 });
  });

  it("does not connect when disabled", () => {
    const h = makeHarness();
    renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "markdown",
        enabled: false,
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );
    expect(h.providerFactory).not.toHaveBeenCalled();
  });

  it("tears down the provider and doc on unmount", () => {
    const h = makeHarness();
    const { unmount } = renderHook(() =>
      useSpecCollab("SPEC-1", {
        field: "markdown",
        providerFactory: h.providerFactory,
        docFactory: h.docFactory,
      }),
    );
    unmount();
    expect(h.provider.disconnect).toHaveBeenCalled();
    expect(h.provider.destroy).toHaveBeenCalled();
  });
});
