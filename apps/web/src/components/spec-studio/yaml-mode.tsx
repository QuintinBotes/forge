"use client";

import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { useMemo, useRef, useState, type ChangeEvent, type ReactNode, type UIEvent } from "react";

import { cn } from "@/lib/utils";
import { hasErrors, validateManifestYaml } from "@/lib/spec-studio/yaml-schema";
import { Button } from "@/components/ui/button";

export interface YamlModeProps {
  /** The current `manifest.yaml` text (controlled). */
  value: string;
  onChange: (next: string) => void;
  onSave: () => void;
  saving?: boolean;
  /** True once `value` differs from the last saved/loaded text. */
  dirty?: boolean;
  saveError?: string | null;
  /** Live-collaboration presence bar rendered in the header (CRDT mode). */
  presence?: ReactNode;
  /** Report the local cursor/selection for remote presence (CRDT mode). */
  onSelectionChange?: (anchor: number, head: number) => void;
}

/**
 * The YAML manifest editor — Spec Studio's 4th mode. A schema-aware
 * `manifest.yaml` editor: JetBrains Mono, a line-number gutter, and live
 * client-side validation with line-anchored errors (see
 * `lib/spec-studio/yaml-schema`). Edits are the same `SpecManifest` the
 * Guided and Markdown modes edit — this only changes the surface.
 */
export function YamlMode({
  value,
  onChange,
  onSave,
  saving = false,
  dirty = false,
  saveError,
  presence,
  onSelectionChange,
}: YamlModeProps) {
  const issues = useMemo(() => validateManifestYaml(value), [value]);
  const invalid = hasErrors(issues);
  const lineCount = useMemo(() => Math.max(1, value.split("\n").length), [value]);
  const gutterRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const onScroll = (event: UIEvent<HTMLTextAreaElement>) => {
    setScrollTop(event.currentTarget.scrollTop);
  };

  const onTextChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(event.target.value);
  };

  return (
    <div className="flex flex-col gap-3" data-testid="yaml-mode">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {invalid ? (
            <span className="inline-flex items-center gap-1 text-danger" data-testid="yaml-status-invalid">
              <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
              {issues.filter((i) => i.severity === "error").length} error
              {issues.filter((i) => i.severity === "error").length === 1 ? "" : "s"}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-success" data-testid="yaml-status-valid">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
              Valid manifest
            </span>
          )}
          {dirty ? <span className="text-muted-foreground/70">Unsaved changes</span> : null}
          {presence}
        </div>
        <Button
          size="sm"
          onClick={onSave}
          disabled={invalid || saving || !dirty}
          data-testid="yaml-save"
        >
          {saving ? "Saving…" : "Save manifest.yaml"}
        </Button>
      </div>

      <div className="flex overflow-hidden rounded-lg border border-border bg-card">
        <div
          ref={gutterRef}
          aria-hidden
          className="select-none overflow-hidden border-r border-border bg-muted/40 px-3 py-3 text-right font-mono text-xs leading-5 text-muted-foreground/70"
          style={{ transform: `translateY(-${scrollTop}px)` }}
        >
          {Array.from({ length: lineCount }, (_, i) => (
            <div key={i}>{i + 1}</div>
          ))}
        </div>
        <textarea
          data-testid="yaml-textarea"
          aria-label="manifest.yaml"
          spellCheck={false}
          value={value}
          onChange={onTextChange}
          onScroll={onScroll}
          onSelect={(event) =>
            onSelectionChange?.(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
          }
          className="min-h-[24rem] flex-1 resize-none bg-transparent px-3 py-3 font-mono text-xs leading-5 text-foreground outline-none"
        />
      </div>

      {saveError ? (
        <p role="alert" className="text-xs text-danger" data-testid="yaml-save-error">
          {saveError}
        </p>
      ) : null}

      {issues.length > 0 ? (
        <ul className="flex flex-col gap-1" data-testid="yaml-issues" aria-label="Manifest validation issues">
          {issues.map((issue, index) => (
            <li key={`${issue.line}-${index}`}>
              <button
                type="button"
                className={cn(
                  "flex w-full items-start gap-2 rounded-md border px-3 py-1.5 text-left text-xs",
                  issue.severity === "error"
                    ? "border-danger/30 bg-danger/5 text-danger"
                    : "border-warning/30 bg-warning/5 text-warning",
                )}
                onClick={() => focusLine(gutterRef, issue.line)}
              >
                <span className="font-mono text-[11px] shrink-0">
                  Ln {issue.line}
                  {issue.column ? `:${issue.column}` : ""}
                </span>
                <span>{issue.message}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function focusLine(gutterRef: React.RefObject<HTMLDivElement | null>, line: number) {
  const container = gutterRef.current?.parentElement;
  const textarea = container?.querySelector("textarea");
  if (!textarea) return;
  const value = textarea.value;
  const lines = value.split("\n");
  let offset = 0;
  for (let i = 0; i < line - 1 && i < lines.length; i += 1) {
    offset += lines[i].length + 1;
  }
  textarea.focus();
  textarea.setSelectionRange(offset, offset);
}
