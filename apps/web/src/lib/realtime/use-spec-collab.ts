"use client";

/**
 * Collaborative spec-editing hook (Yjs / y-websocket).
 *
 * Spec Studio's Markdown (`spec.md`) and YAML (`manifest.yaml`) surfaces switch
 * from whole-document PUT-on-save to CRDT-synced editing: every keystroke is a
 * Yjs delta on a shared `Y.Text`, merged conflict-free across editors. The
 * `Y.Doc` is *ephemeral session state* — the server materialises it back through
 * `FileSpecEngine` on quiesce, which stays the canonical store (and keeps the
 * `SpecVersion` history). Guided mode + the legacy PUT endpoints keep working
 * for single-editor / API clients.
 *
 * Like the board hook's `socketFactory`, the provider and `Y.Doc` are created
 * through an **injectable factory** so vitest/jsdom needs no real WebSocket:
 * pass `providerFactory` (and optionally `docFactory`) a fake in tests.
 *
 * Presence/cursor colours come from the design-token chart ramp
 * (`hsl(var(--chart-N))`) — never a hardcoded hex.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { WebsocketProvider } from "y-websocket";
import * as Y from "yjs";

import { resolveSpecWsBaseUrl } from "./ws-url";

/** The two CRDT-synced spec surfaces; each is a named `Y.Text` in the doc. */
export type SpecCollabField = "markdown" | "yaml";

/**
 * Root `Y.Text` key each surface binds — the **cross-stack contract** with the
 * server room. The authoritative pycrdt doc keys its shared texts as `spec.md`
 * and `manifest.yaml` (see `spec_room.py` `SPEC_MD_KEY` / `MANIFEST_YAML_KEY`)
 * and materialises them back through `FileSpecEngine`. In Yjs / y-crdt interop,
 * root-level shared types are addressed by name, so the client MUST select the
 * same key or its edits land in a root type the server never reads — no
 * convergence. Do not rename without changing the server in lock-step.
 */
export const SPEC_COLLAB_DOC_KEYS: Record<SpecCollabField, string> = {
  markdown: "spec.md",
  yaml: "manifest.yaml",
};

/** Structural subset of `y-protocols` `Awareness` used by this hook. */
export interface AwarenessLike {
  clientID: number;
  getLocalState(): Record<string, unknown> | null;
  setLocalState(state: Record<string, unknown> | null): void;
  setLocalStateField(field: string, value: unknown): void;
  getStates(): Map<number, Record<string, unknown>>;
  on(event: "change" | "update", cb: () => void): void;
  off(event: "change" | "update", cb: () => void): void;
}

/** Structural subset of a `y-websocket` `WebsocketProvider`. */
export interface CollabProvider {
  awareness: AwarenessLike;
  on(event: "status" | "sync", cb: (payload: unknown) => void): void;
  off(event: "status" | "sync", cb: (payload: unknown) => void): void;
  disconnect?(): void;
  destroy(): void;
}

export interface ProviderFactoryArgs {
  /** Base WS URL, e.g. `ws://localhost:8000/ws/spec` (room appended). */
  url: string;
  /** Room name — the spec id. */
  room: string;
  doc: Y.Doc;
  /** Query params merged onto the socket URL (carries `token`). */
  params: Record<string, string>;
}

export type ProviderFactory = (args: ProviderFactoryArgs) => CollabProvider;
export type DocFactory = () => Y.Doc;

/** Local editor identity broadcast over awareness. */
export interface CollabUser {
  /** Display name shown to other editors. */
  name: string;
}

/** A remote (or local) editor's live presence. */
export interface CollabPeer {
  clientId: number;
  name: string;
  /** Cursor colour as a design-token reference, e.g. `hsl(var(--chart-3))`. */
  colorVar: string;
  /** Selection offsets into the shared text, if the peer has focus. */
  cursor: { anchor: number; head: number } | null;
  /** True for the local editor's own presence entry. */
  isSelf: boolean;
}

export interface UseSpecCollabOptions {
  /** Which shared `Y.Text` to bind (`markdown` → `spec.md`, `yaml` → manifest). */
  field: SpecCollabField;
  /** Gate the connection (mirrors the board hook's `enabled`). */
  enabled?: boolean;
  /** Bearer token forwarded as the `?token=` query param (WS auth). */
  token?: string;
  /**
   * Base WS URL. Defaults to `NEXT_PUBLIC_SPEC_WS_URL` → `NEXT_PUBLIC_WS_URL`
   * (`/ws`→`/ws/spec`) → the same-origin `/ws/spec` derived from the page's
   * `window.location` (see `resolveSpecWsBaseUrl`).
   */
  baseUrl?: string;
  /** Local editor identity for presence. */
  user?: CollabUser;
  /**
   * Text to seed the shared doc with on first sync **if it is still empty**
   * (e.g. the last saved `spec.md` loaded over REST). Lets the collaborative
   * editor be usable before the server materialises the doc from
   * `FileSpecEngine`; a non-empty synced doc always wins.
   */
  seedText?: string;
  /** Inject a provider (tests); defaults to a real `WebsocketProvider`. */
  providerFactory?: ProviderFactory;
  /** Inject a `Y.Doc` (tests); defaults to `new Y.Doc()`. */
  docFactory?: DocFactory;
}

export interface SpecCollabState {
  /** Current shared text (re-renders on every remote or local delta). */
  text: string;
  /** Replace the shared text from a controlled input (minimal CRDT delta). */
  setText: (next: string) => void;
  /** Publish the local cursor/selection over awareness. */
  setSelection: (anchor: number, head: number) => void;
  /** Transport connected. */
  connected: boolean;
  /** Initial document state synced from the server. */
  synced: boolean;
  /** Everyone currently in the room, including the local editor (`isSelf`). */
  peers: CollabPeer[];
}

/** Number of slots in the categorical chart ramp (`--chart-1..6`). */
const CHART_SLOTS = 6;

/**
 * Deterministic cursor colour for a client, drawn from the design-token chart
 * ramp so light/dark themes and colourblind-safety tuning are inherited. Never
 * a hardcoded hex (repo convention).
 */
export function cursorColorVar(clientId: number): string {
  const slot = (Math.abs(clientId) % CHART_SLOTS) + 1;
  return `hsl(var(--chart-${slot}))`;
}

function defaultDocFactory(): Y.Doc {
  return new Y.Doc();
}

/**
 * Apply a controlled-input string to a `Y.Text` as a minimal prefix/suffix
 * delta, so a full-value `onChange` (textarea) still produces a targeted CRDT
 * edit that merges with concurrent remote edits instead of clobbering them.
 */
function replaceYText(ytext: Y.Text, next: string): void {
  const prev = ytext.toString();
  if (prev === next) return;
  let start = 0;
  const min = Math.min(prev.length, next.length);
  while (start < min && prev[start] === next[start]) start += 1;
  let endPrev = prev.length;
  let endNext = next.length;
  while (endPrev > start && endNext > start && prev[endPrev - 1] === next[endNext - 1]) {
    endPrev -= 1;
    endNext -= 1;
  }
  const doc = ytext.doc;
  const mutate = () => {
    if (endPrev > start) ytext.delete(start, endPrev - start);
    if (endNext > start) ytext.insert(start, next.slice(start, endNext));
  };
  if (doc) doc.transact(mutate);
  else mutate();
}

function readPeers(awareness: AwarenessLike): CollabPeer[] {
  const self = awareness.clientID;
  const peers: CollabPeer[] = [];
  for (const [clientId, state] of awareness.getStates()) {
    const user = (state.user ?? {}) as { name?: unknown };
    const cursorRaw = state.cursor as { anchor?: unknown; head?: unknown } | undefined;
    const cursor =
      cursorRaw && typeof cursorRaw.anchor === "number" && typeof cursorRaw.head === "number"
        ? { anchor: cursorRaw.anchor, head: cursorRaw.head }
        : null;
    peers.push({
      clientId,
      name: typeof user.name === "string" && user.name ? user.name : "Anonymous",
      colorVar: cursorColorVar(clientId),
      cursor,
      isSelf: clientId === self,
    });
  }
  return peers;
}

/**
 * Bind one spec surface to a shared `Y.Text`. Call once per active editor
 * (Spec Studio mounts only one of Markdown/YAML at a time, so at most one live
 * provider exists). Returns the controlled text plus live presence.
 */
export function useSpecCollab(
  specId: string,
  options: UseSpecCollabOptions,
): SpecCollabState {
  const {
    field,
    enabled = true,
    token,
    baseUrl = resolveSpecWsBaseUrl(),
    user,
    seedText,
    providerFactory,
    docFactory,
  } = options;

  const [text, setTextState] = useState("");
  const [connected, setConnected] = useState(false);
  const [synced, setSynced] = useState(false);
  const [peers, setPeers] = useState<CollabPeer[]>([]);

  // Live refs the imperative callbacks read without re-subscribing.
  const ytextRef = useRef<Y.Text | null>(null);
  const awarenessRef = useRef<AwarenessLike | null>(null);
  // Seed text is read at sync time only; keep it out of the connection effect's
  // deps so a late-loading REST value never tears down a live connection.
  const seedTextRef = useRef(seedText);
  useEffect(() => {
    seedTextRef.current = seedText;
  }, [seedText]);

  const userName = user?.name;

  useEffect(() => {
    if (!enabled || !specId) {
      return;
    }

    const makeDoc = docFactory ?? defaultDocFactory;
    const doc = makeDoc();
    // Select by the server's doc key (`spec.md` / `manifest.yaml`), NOT the raw
    // field name — otherwise the client's root shared type never merges with the
    // server-seeded, engine-materialised text (no convergence).
    const ytext = doc.getText(SPEC_COLLAB_DOC_KEYS[field]);
    ytextRef.current = ytext;

    let provider: CollabProvider;
    try {
      const factory = providerFactory ?? defaultProviderFactory;
      provider = factory({
        url: baseUrl,
        room: specId,
        doc,
        params: token ? { token } : {},
      });
    } catch {
      ytextRef.current = null;
      doc.destroy();
      return;
    }

    const awareness = provider.awareness;
    awarenessRef.current = awareness;
    awareness.setLocalStateField("user", { name: userName ?? "Anonymous" });

    let seeded = false;
    const maybeSeed = () => {
      if (seeded) return;
      seeded = true;
      const seed = seedTextRef.current;
      if (seed && ytext.length === 0) {
        replaceYText(ytext, seed);
      }
    };
    const syncText = () => setTextState(ytext.toString());
    const syncPeers = () => setPeers(readPeers(awareness));
    const handleStatus = (payload: unknown) => {
      const status = (payload as { status?: string } | undefined)?.status;
      setConnected(status === "connected");
    };
    const handleSync = (payload: unknown) => {
      const isSynced = payload === true || payload === undefined;
      setSynced(isSynced);
      if (isSynced) maybeSeed();
      syncText();
    };

    syncText();
    syncPeers();
    ytext.observe(syncText);
    awareness.on("change", syncPeers);
    provider.on("status", handleStatus);
    provider.on("sync", handleSync);

    return () => {
      ytext.unobserve(syncText);
      awareness.off("change", syncPeers);
      provider.off("status", handleStatus);
      provider.off("sync", handleSync);
      awareness.setLocalState(null);
      provider.disconnect?.();
      provider.destroy();
      doc.destroy();
      ytextRef.current = null;
      awarenessRef.current = null;
      setConnected(false);
      setSynced(false);
      setPeers([]);
      setTextState("");
    };
  }, [enabled, specId, field, baseUrl, token, userName, providerFactory, docFactory]);

  const setText = useCallback((next: string) => {
    const ytext = ytextRef.current;
    if (ytext) replaceYText(ytext, next);
  }, []);

  const setSelection = useCallback((anchor: number, head: number) => {
    awarenessRef.current?.setLocalStateField("cursor", { anchor, head });
  }, []);

  return useMemo(
    () => ({ text, setText, setSelection, connected, synced, peers }),
    [text, setText, setSelection, connected, synced, peers],
  );
}

/**
 * Default provider: a real `y-websocket` `WebsocketProvider`. Only reached in
 * the browser — jsdom/vitest inject `providerFactory`, so the real
 * `WebSocket`-backed connection is never opened under test.
 */
function defaultProviderFactory(args: ProviderFactoryArgs): CollabProvider {
  const provider = new WebsocketProvider(args.url, args.room, args.doc, {
    params: args.params,
  });
  return provider as unknown as CollabProvider;
}
