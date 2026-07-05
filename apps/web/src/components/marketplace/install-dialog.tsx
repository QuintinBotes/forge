"use client";

import {
  AlertTriangle,
  Ban,
  Download,
  Loader2,
  ShieldAlert,
} from "lucide-react";
import { useEffect, useState } from "react";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import {
  useInstallPackage,
  usePreviewInstall,
} from "@/lib/api/marketplace";
import type { InstallResult, Listing } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { HashChip, VerificationBadge } from "./marketplace-badges";
import { isBlocked, kindLabel, needsAcknowledgement } from "./marketplace-meta";

export interface InstallDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  listing: Listing;
  /** Install a specific version; defaults to the listing's latest. */
  version?: string;
  client?: ForgeApiClient;
  onInstalled?: (result: InstallResult) => void;
}

/** Read a human-readable block reason out of a 422 (or any) preview failure. */
function blockReasonFrom(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown } | null;
    const detail = body?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message);
    }
    if (typeof detail === "string") return detail;
  }
  if (error instanceof Error) return error.message;
  return "Preview failed.";
}

/**
 * The install trust boundary. On open it dry-runs a `preview` and surfaces the
 * assay: the verification hallmark, resolved version, warnings and any required
 * admin follow-up. Hard-blocked packages (bad signature / hash) cannot be
 * installed; soft-gated ones (unsigned / untrusted registry) require an explicit
 * acknowledgement before the single ember "Install" action is armed.
 */
export function InstallDialog({
  open,
  onOpenChange,
  listing,
  version,
  client,
  onInstalled,
}: InstallDialogProps) {
  const [acknowledged, setAcknowledged] = useState(false);

  const preview = usePreviewInstall(client);
  const install = useInstallPackage(client);
  const { mutate: runPreview, reset: resetPreview } = preview;

  const resolvedVersion = version ?? listing.latest_version;

  // Dry-run the install whenever the dialog opens (or the target changes).
  useEffect(() => {
    if (!open) return;
    setAcknowledged(false);
    resetPreview();
    runPreview({
      registry_id: listing.registry_id,
      kind: listing.kind,
      slug: listing.slug,
      version: resolvedVersion,
    });
  }, [
    open,
    listing.registry_id,
    listing.kind,
    listing.slug,
    resolvedVersion,
    runPreview,
    resetPreview,
  ]);

  const plan = preview.data;
  const verification = plan?.verification.status;
  const hardBlocked =
    (plan?.blocked ?? false) ||
    (verification ? isBlocked(verification) : false) ||
    (preview.isError && preview.error instanceof ApiError
      ? preview.error.status === 422
      : false);
  const ackRequired = verification ? needsAcknowledgement(verification) : false;
  const canInstall =
    Boolean(plan) && !hardBlocked && (!ackRequired || acknowledged);

  const onConfirm = () => {
    if (!canInstall) return;
    install.mutate(
      {
        registry_id: listing.registry_id,
        kind: listing.kind,
        slug: listing.slug,
        version: resolvedVersion,
        acknowledge_unverified: acknowledged,
      },
      {
        onSuccess: (result) => {
          onInstalled?.(result);
          onOpenChange(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="install-dialog"
        className="max-w-md gap-5"
        aria-describedby="install-dialog-desc"
      >
        <DialogHeader>
          <DialogTitle className="font-display">
            Install {listing.name}
          </DialogTitle>
          <DialogDescription id="install-dialog-desc">
            {kindLabel(listing.kind)} · {listing.registry_slug} ·{" "}
            <span className="font-mono">v{resolvedVersion}</span>
          </DialogDescription>
        </DialogHeader>

        {preview.isPending ? (
          <div
            data-testid="preview-loading"
            className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-4 text-sm text-muted-foreground"
          >
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Assaying package — verifying content hash and signature…
          </div>
        ) : hardBlocked ? (
          <div
            role="alert"
            data-testid="install-blocked"
            className="flex flex-col gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-3 text-sm text-danger"
          >
            <div className="flex items-center gap-2 font-medium">
              <Ban className="h-4 w-4" aria-hidden />
              Installation blocked
            </div>
            <p className="text-danger/90">
              {plan?.block_reason ?? blockReasonFrom(preview.error)}
            </p>
            {verification ? <VerificationBadge status={verification} /> : null}
          </div>
        ) : preview.isError ? (
          <div
            role="alert"
            data-testid="preview-error"
            className="flex flex-col gap-2 rounded-md border border-border bg-muted/40 px-3 py-3 text-sm text-muted-foreground"
          >
            <div className="flex items-center gap-2 font-medium text-foreground">
              <AlertTriangle className="h-4 w-4 text-warning" aria-hidden />
              Preview unavailable
            </div>
            <p>{blockReasonFrom(preview.error)}</p>
          </div>
        ) : plan ? (
          <div className="flex flex-col gap-4 text-sm">
            {/* The struck hallmark. */}
            <div className="flex flex-wrap items-center gap-2">
              {verification ? <VerificationBadge status={verification} /> : null}
              <HashChip
                hash={
                  typeof plan.resolved_config.content_hash === "string"
                    ? (plan.resolved_config.content_hash as string)
                    : ""
                }
              />
            </div>

            {plan.warnings.length > 0 ? (
              <ul
                data-testid="plan-warnings"
                className="flex flex-col gap-1.5 rounded-md border border-warning/30 bg-warning/5 px-3 py-2"
              >
                {plan.warnings.map((w) => (
                  <li
                    key={w}
                    className="flex items-start gap-2 text-xs text-foreground"
                  >
                    <AlertTriangle
                      className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning"
                      aria-hidden
                    />
                    {w}
                  </li>
                ))}
              </ul>
            ) : null}

            {plan.requires_admin_followup.length > 0 ? (
              <div
                data-testid="plan-followup"
                className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/40 px-3 py-2"
              >
                <p className="text-xs font-medium text-muted-foreground">
                  Admin follow-up after install
                </p>
                <ul className="flex flex-col gap-1">
                  {plan.requires_admin_followup.map((f) => (
                    <li
                      key={f}
                      className="font-mono text-[11px] text-muted-foreground"
                    >
                      {f}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {ackRequired ? (
              <label
                data-testid="ack-unverified"
                className={cn(
                  "flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 text-xs",
                  "border-warning/40 bg-warning/5 text-foreground",
                )}
              >
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-primary"
                  aria-label="Acknowledge unverified package"
                />
                <span className="flex items-center gap-1.5">
                  <ShieldAlert
                    className="h-3.5 w-3.5 shrink-0 text-warning"
                    aria-hidden
                  />
                  I understand this package is not cryptographically verified and
                  choose to install it anyway.
                </span>
              </label>
            ) : null}

            {install.isError ? (
              <p role="alert" className="text-xs text-danger">
                Install failed — {blockReasonFrom(install.error)}
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={install.isPending}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            data-testid="confirm-install"
            onClick={onConfirm}
            disabled={!canInstall || install.isPending}
          >
            <Download className="h-4 w-4" aria-hidden />
            {install.isPending ? "Installing…" : "Install"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
