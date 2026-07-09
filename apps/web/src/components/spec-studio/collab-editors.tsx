"use client";

import {
  useSpecCollab,
  type DocFactory,
  type ProviderFactory,
  type CollabUser,
} from "@/lib/realtime/use-spec-collab";

import { CollabPresence } from "./collab-presence";
import { MarkdownMode } from "./markdown-mode";
import { YamlMode } from "./yaml-mode";

/** Shared live-collaboration configuration threaded from Spec Studio. */
export interface SpecCollabConfig {
  /** Turn CRDT editing on; when false/absent the modes stay PUT-on-save. */
  enabled?: boolean;
  /** Bearer token for the `?token=` WS auth query param. */
  token?: string;
  /** Local editor identity for presence. */
  user?: CollabUser;
  /** Override the base `/ws/spec` URL. */
  baseUrl?: string;
  /** Inject a provider (tests). */
  providerFactory?: ProviderFactory;
  /** Inject a `Y.Doc` (tests). */
  docFactory?: DocFactory;
}

interface CollabEditorBaseProps {
  specId: string;
  collab: SpecCollabConfig;
  /** Last text loaded/saved over REST — seeds the doc and drives `dirty`. */
  savedText: string;
  onSave: (text: string) => void;
  saving: boolean;
  saveError: string | null;
}

/**
 * `spec.md` bound to a shared `Y.Text` over y-websocket: every keystroke is a
 * CRDT delta merged across editors, with a live presence bar. The Save button
 * still materialises the current text through the legacy PUT path so
 * single-editor/API clients keep working.
 */
export function CollabMarkdownMode({
  specId,
  collab,
  savedText,
  onSave,
  saving,
  saveError,
}: CollabEditorBaseProps) {
  const { text, setText, setSelection, connected, synced, peers } = useSpecCollab(specId, {
    field: "markdown",
    enabled: collab.enabled,
    token: collab.token,
    baseUrl: collab.baseUrl,
    user: collab.user,
    seedText: savedText,
    providerFactory: collab.providerFactory,
    docFactory: collab.docFactory,
  });

  return (
    <MarkdownMode
      value={text}
      onChange={setText}
      onSave={() => onSave(text)}
      saving={saving}
      dirty={text !== savedText}
      saveError={saveError}
      onSelectionChange={setSelection}
      presence={<CollabPresence peers={peers} connected={connected} synced={synced} />}
    />
  );
}

/**
 * `manifest.yaml` bound to a shared `Y.Text` — the YAML twin of
 * {@link CollabMarkdownMode}.
 */
export function CollabYamlMode({
  specId,
  collab,
  savedText,
  onSave,
  saving,
  saveError,
}: CollabEditorBaseProps) {
  const { text, setText, setSelection, connected, synced, peers } = useSpecCollab(specId, {
    field: "yaml",
    enabled: collab.enabled,
    token: collab.token,
    baseUrl: collab.baseUrl,
    user: collab.user,
    seedText: savedText,
    providerFactory: collab.providerFactory,
    docFactory: collab.docFactory,
  });

  return (
    <YamlMode
      value={text}
      onChange={setText}
      onSave={() => onSave(text)}
      saving={saving}
      dirty={text !== savedText}
      saveError={saveError}
      onSelectionChange={setSelection}
      presence={<CollabPresence peers={peers} connected={connected} synced={synced} />}
    />
  );
}
