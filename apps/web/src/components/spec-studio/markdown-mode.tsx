"use client";

import { Button } from "@/components/ui/button";

export interface MarkdownModeProps {
  /** The current `spec.md` text (controlled). */
  value: string;
  onChange: (next: string) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
  saveError?: string | null;
}

/**
 * The `spec.md` prose editor — Spec Studio's default human/agent surface.
 * A plain textarea (no schema gate: prose is forgiving); saving re-renders
 * `manifest.yaml` to match on the backend.
 */
export function MarkdownMode({
  value,
  onChange,
  onSave,
  saving = false,
  dirty = false,
  saveError,
}: MarkdownModeProps) {
  return (
    <div className="flex flex-col gap-3" data-testid="markdown-mode">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          {dirty ? "Unsaved changes" : "spec.md"}
        </span>
        <Button size="sm" onClick={onSave} disabled={saving || !dirty} data-testid="markdown-save">
          {saving ? "Saving…" : "Save spec.md"}
        </Button>
      </div>
      <textarea
        data-testid="markdown-textarea"
        aria-label="spec.md"
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-[24rem] resize-none rounded-lg border border-border bg-card px-3 py-3 font-mono text-xs leading-5 text-foreground outline-none"
      />
      {saveError ? (
        <p role="alert" className="text-xs text-danger" data-testid="markdown-save-error">
          {saveError}
        </p>
      ) : null}
    </div>
  );
}
