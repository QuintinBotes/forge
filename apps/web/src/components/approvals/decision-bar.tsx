"use client";

import {
  ArrowUpCircle,
  Check,
  MessageSquare,
  X,
  type LucideIcon,
} from "lucide-react";
import type { KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import type { ApprovalAction } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { ACTION_META, ACTION_ORDER } from "./approval-meta";

const ACTION_ICON: Record<ApprovalAction, LucideIcon> = {
  approve: Check,
  reject: X,
  request_changes: MessageSquare,
  escalate: ArrowUpCircle,
};

const NOTE_PROMPT: Record<"reject" | "request_changes", string> = {
  reject: "Reason for rejecting",
  request_changes: "What needs to change?",
};

export interface DecisionBarProps {
  /** Item 9 — the gate-correct available actions. */
  actions: ApprovalAction[];
  /** The action whose reason is being composed (`reject`/`request_changes`). */
  activeNote: "reject" | "request_changes" | null;
  note: string;
  onNoteChange: (value: string) => void;
  pending: boolean;
  disabled?: boolean;
  errorMessage?: string | null;
  onTrigger: (action: ApprovalAction) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * The decision surface (must-show item 9). Approve is the single ember primary;
 * reject/request-changes reveal an inline reason composer (keyboard-first: the
 * `a/r/x/e` map lives in {@link ApprovalInbox}). Cmd/Ctrl+Enter confirms a
 * composed reason, Escape cancels.
 */
export function DecisionBar({
  actions,
  activeNote,
  note,
  onNoteChange,
  pending,
  disabled = false,
  errorMessage,
  onTrigger,
  onConfirm,
  onCancel,
}: DecisionBarProps) {
  const ordered = ACTION_ORDER.filter((a) => actions.includes(a));

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
    } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      onConfirm();
    }
  };

  return (
    <div
      data-testid="decision-bar"
      className="border-t border-border bg-card/80 px-6 py-3 backdrop-blur"
    >
      {errorMessage ? (
        <p role="alert" className="mb-2 text-xs font-medium text-danger">
          {errorMessage}
        </p>
      ) : null}

      {activeNote ? (
        <div className="flex flex-col gap-2" data-testid="reason-composer">
          <label
            htmlFor="decision-note"
            className="text-xs font-medium text-muted-foreground"
          >
            {NOTE_PROMPT[activeNote]}
          </label>
          <textarea
            id="decision-note"
            autoFocus
            rows={2}
            value={note}
            onChange={(e) => onNoteChange(e.target.value)}
            onKeyDown={onComposerKeyDown}
            placeholder="Add a note for the requester…"
            className="w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={onCancel} disabled={pending}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant={activeNote === "reject" ? "outline" : "secondary"}
              className={cn(
                activeNote === "reject" &&
                  "border-danger/50 text-danger hover:bg-danger/10 hover:text-danger",
              )}
              onClick={onConfirm}
              disabled={pending}
              data-testid="confirm-decision"
            >
              {activeNote === "reject" ? "Confirm reject" : "Send request"}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {ordered.map((action) => {
            const meta = ACTION_META[action];
            const Icon = ACTION_ICON[action];
            const isApprove = action === "approve";
            const isReject = action === "reject";
            return (
              <Button
                key={action}
                size="sm"
                variant={
                  isApprove ? "default" : isReject ? "outline" : "ghost"
                }
                disabled={disabled || pending}
                onClick={() => onTrigger(action)}
                data-testid={`decision-${action}`}
                className={cn(
                  !isApprove && "text-muted-foreground",
                  isReject &&
                    "border-danger/40 text-danger hover:bg-danger/10 hover:text-danger",
                )}
              >
                <Icon aria-hidden />
                {meta.label}
                <kbd className="ml-1 rounded border border-current/30 px-1 font-mono text-[10px] uppercase opacity-70">
                  {meta.shortcut}
                </kbd>
              </Button>
            );
          })}
        </div>
      )}
    </div>
  );
}
