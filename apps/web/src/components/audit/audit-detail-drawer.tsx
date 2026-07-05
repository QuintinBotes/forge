"use client";

import { ArrowRight, Fingerprint, Hash } from "lucide-react";
import type { ReactNode } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { AuditEntry } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { OutcomeBadge, SeverityBadge } from "./audit-badges";
import {
  absoluteTime,
  actionLabel,
  actionMeta,
  actionNamespace,
  actorDisplay,
  actorTypeMeta,
  humanize,
  redactedJsonString,
  relativeTime,
  shortId,
} from "./audit-meta";

export interface AuditDetailDrawerProps {
  entry: AuditEntry | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * The right-anchored detail sheet for one audit entry: actor, resource, the
 * before→after change, structured details and the hash-chain integrity fields.
 * Every payload is re-redacted on render so no credential material ever paints.
 */
export function AuditDetailDrawer({
  entry,
  open,
  onOpenChange,
}: AuditDetailDrawerProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="audit-drawer"
        className="left-auto right-0 top-0 h-full max-h-screen w-full max-w-xl translate-x-0 translate-y-0 gap-0 overflow-y-auto rounded-none border-l p-0 sm:rounded-none"
      >
        {entry ? <DrawerBody entry={entry} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function DrawerBody({ entry }: { entry: AuditEntry }) {
  const ActionIcon = actionMeta(entry.action).icon;
  const ActorIcon = actorTypeMeta(entry.actor_type).icon;
  const hasChange = Boolean(entry.before || entry.after);
  const hasDetails = entry.details && Object.keys(entry.details).length > 0;

  return (
    <>
      <DialogHeader className="space-y-3 border-b border-border p-5 text-left">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <ActionIcon aria-hidden className="h-3.5 w-3.5" />
          <span className="font-mono">{actionNamespace(entry.action)}</span>
        </div>
        <DialogTitle className="font-display text-lg tracking-tight">
          {actionLabel(entry.action)}
        </DialogTitle>
        <DialogDescription className="sr-only">
          Audit entry detail. All secret values are redacted.
        </DialogDescription>
        <div className="flex flex-wrap items-center gap-2">
          <OutcomeBadge result={entry.result} />
          <SeverityBadge severity={entry.severity} />
          <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
            {entry.action}
          </code>
        </div>
        <p className="text-xs text-muted-foreground" title={absoluteTime(entry.created_at)}>
          {relativeTime(entry.created_at)} · {absoluteTime(entry.created_at)}
        </p>
      </DialogHeader>

      <div className="flex flex-col gap-6 p-5">
        {/* Who + what */}
        <Section title="Actor">
          <div className="flex items-center gap-2">
            <ActorIcon aria-hidden className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium text-foreground">
              {actorDisplay(entry)}
            </span>
            <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
              {actorTypeMeta(entry.actor_type).label}
            </span>
          </div>
          {entry.actor_id ? (
            <Field label="Actor ID" mono>
              {entry.actor_id}
            </Field>
          ) : null}
        </Section>

        {(entry.target_type || entry.scope_type) && (
          <Section title="Resource">
            {entry.target_type ? (
              <Field label="Target">
                <span className="text-foreground">{humanize(entry.target_type)}</span>
                {entry.target_id ? (
                  <span className="ml-2 font-mono text-xs text-muted-foreground">
                    {shortId(entry.target_id)}
                  </span>
                ) : null}
              </Field>
            ) : null}
            {entry.scope_type ? (
              <Field label="Scope">
                <span className="text-foreground">{humanize(entry.scope_type)}</span>
                {entry.scope_id ? (
                  <span className="ml-2 font-mono text-xs text-muted-foreground">
                    {shortId(entry.scope_id)}
                  </span>
                ) : null}
              </Field>
            ) : null}
          </Section>
        )}

        {entry.reason ? (
          <Section title="Reason">
            <p className="text-sm text-foreground">{entry.reason}</p>
          </Section>
        ) : null}

        {/* Change diff */}
        {hasChange ? (
          <Section title="Change">
            <div
              data-testid="audit-change"
              className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_auto_1fr] sm:items-center"
            >
              <JsonBlock label="Before" value={entry.before} tone="danger" />
              <ArrowRight
                aria-hidden
                className="mx-auto hidden h-4 w-4 text-muted-foreground sm:block"
              />
              <JsonBlock label="After" value={entry.after} tone="success" />
            </div>
          </Section>
        ) : null}

        {/* Details */}
        {hasDetails ? (
          <Section title="Details">
            <JsonBlock value={entry.details} />
          </Section>
        ) : null}

        {/* Integrity */}
        <Section
          title="Integrity"
          icon={<Fingerprint aria-hidden className="h-3.5 w-3.5" />}
        >
          <p className="mb-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/60 px-2 py-1 text-[11px] text-muted-foreground">
            <Hash aria-hidden className="h-3 w-3" />
            Hash-chained · tamper-evident · immutable
          </p>
          {entry.seq != null ? (
            <Field label="Sequence" mono>
              #{entry.seq}
            </Field>
          ) : null}
          {entry.request_id ? (
            <Field label="Request ID" mono>
              {entry.request_id}
            </Field>
          ) : null}
          {entry.entry_hash ? (
            <Field label="Entry hash" mono wrap>
              {entry.entry_hash}
            </Field>
          ) : null}
          {entry.prev_hash ? (
            <Field label="Prev hash" mono wrap>
              {entry.prev_hash}
            </Field>
          ) : null}
          {entry.payload_hash ? (
            <Field label="Payload hash" mono wrap>
              {entry.payload_hash}
            </Field>
          ) : null}
        </Section>
      </div>
    </>
  );
}

// --- Layout helpers ------------------------------------------------------- //

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

function Field({
  label,
  children,
  mono,
  wrap,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  wrap?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5 text-sm sm:flex-row sm:items-baseline sm:gap-3">
      <span className="w-28 shrink-0 text-xs text-muted-foreground">{label}</span>
      <span
        className={cn(
          "min-w-0 text-foreground",
          mono && "font-mono text-xs",
          wrap ? "break-all" : "truncate",
        )}
      >
        {children}
      </span>
    </div>
  );
}

function JsonBlock({
  label,
  value,
  tone,
}: {
  label?: string;
  value: unknown;
  tone?: "danger" | "success";
}) {
  const empty = value == null || (typeof value === "object" && Object.keys(value).length === 0);
  return (
    <div className="min-w-0">
      {label ? (
        <p
          className={cn(
            "mb-1 text-[11px] font-medium",
            tone === "danger" && "text-danger",
            tone === "success" && "text-success",
            !tone && "text-muted-foreground",
          )}
        >
          {label}
        </p>
      ) : null}
      <pre className="overflow-x-auto rounded-md border border-border bg-muted/50 p-3 font-mono text-[11px] leading-relaxed text-foreground">
        {empty ? "—" : redactedJsonString(value)}
      </pre>
    </div>
  );
}
