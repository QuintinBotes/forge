"use client";

/**
 * Shared, presentational primitives for the RBAC admin panels — a token-only
 * badge, section card, labelled field, inline error note, empty state and a
 * loading skeleton. No data or hooks; panels compose these so the three tabs
 * read as one system.
 */

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** Standard control styling (mirrors the SSO settings form). */
export const FIELD_CLS =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function Badge({
  tone,
  className,
  children,
}: {
  tone?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        tone,
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Dot({ className }: { className?: string }) {
  return (
    <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", className)} />
  );
}

export function SectionCard({
  icon,
  title,
  description,
  actions,
  children,
  testid,
}: {
  icon: ReactNode;
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  testid?: string;
}) {
  return (
    <section
      data-testid={testid}
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            {icon}
          </span>
          <div>
            <h2 className="font-display text-base font-semibold tracking-tight">
              {title}
            </h2>
            {description ? (
              <p className="text-sm text-muted-foreground">{description}</p>
            ) : null}
          </div>
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={htmlFor}
        className="flex items-baseline justify-between gap-2"
      >
        <span className="text-sm font-medium text-foreground">{label}</span>
        {hint ? (
          <span className="text-[11px] text-muted-foreground">{hint}</span>
        ) : null}
      </label>
      {children}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-sm text-danger">
      {children}
    </p>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  action,
  testid,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  testid?: string;
}) {
  return (
    <div
      data-testid={testid}
      className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border px-6 py-12 text-center"
    >
      <span className="flex h-10 w-10 items-center justify-center rounded-full border border-border bg-muted/40 text-muted-foreground">
        {icon}
      </span>
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {children ? (
          <p className="max-w-sm text-xs text-muted-foreground">{children}</p>
        ) : null}
      </div>
      {action}
    </div>
  );
}

export function SkeletonRows({
  rows = 3,
  testid,
}: {
  rows?: number;
  testid?: string;
}) {
  return (
    <div
      data-testid={testid}
      aria-busy="true"
      className="flex flex-col gap-2"
    >
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-14 animate-pulse rounded-lg border border-border bg-muted/40"
        />
      ))}
    </div>
  );
}
