"use client";

import { KeyRound, Plus, ShieldCheck, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useCreateScimToken,
  useRevokeScimToken,
  useScimTokens,
} from "@/lib/api/sso";
import type { ScimTokenCreated, ScimTokenInfo, SsoConfig } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { CopyField } from "./copy-field";
import {
  activeTokenCount,
  formatDateTime,
  formatRelative,
  scimBaseUrl,
  scimTokenStatus,
  type ScimTokenStatus,
} from "./sso-meta";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const STATUS_STYLE: Record<ScimTokenStatus, string> = {
  active: "border-success/40 bg-success/10 text-success",
  revoked: "border-border bg-muted text-muted-foreground",
  expired: "border-warning/40 bg-warning/10 text-warning",
};

export interface ScimPanelProps {
  workspaceId: string;
  config: SsoConfig | null;
  /** API base, used to derive the SCIM endpoint when no SAML config exists yet. */
  apiBaseUrl: string;
  client?: ForgeApiClient;
}

/**
 * SCIM 2.0 provisioning: the endpoint the IdP pushes users/groups to, plus the
 * bearer tokens that authenticate it. A freshly issued token's secret is shown
 * exactly once (the backend never returns it again), so the create flow ends on
 * a copy-it-now panel rather than a silent success.
 */
export function ScimPanel({
  workspaceId,
  config,
  apiBaseUrl,
  client = apiClient,
}: ScimPanelProps) {
  const tokensQuery = useScimTokens(workspaceId, client);
  const revoke = useRevokeScimToken(client);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const tokens = tokensQuery.data ?? [];
  const active = activeTokenCount(tokens);
  const base = scimBaseUrl(config, apiBaseUrl);

  const onRevoke = (token: ScimTokenInfo) => {
    setRevokeError(null);
    setRevoking(token.id);
    revoke.mutate(
      { workspaceId, tokenId: token.id },
      {
        onError: (err) =>
          setRevokeError(
            err instanceof ApiError && err.status === 403
              ? "You don't have permission to revoke SCIM tokens."
              : `Couldn't revoke "${token.name}". Please try again.`,
          ),
        onSettled: () => setRevoking(null),
      },
    );
  };

  return (
    <section
      data-testid="scim-panel"
      aria-labelledby="scim-heading"
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <ShieldCheck className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <div className="flex items-center gap-2">
              <h2
                id="scim-heading"
                className="font-display text-base font-semibold tracking-tight"
              >
                SCIM provisioning
              </h2>
              <StatusPill active={active} />
            </div>
            <p className="text-sm text-muted-foreground">
              Sync users and groups from your identity provider with a bearer
              token.
            </p>
          </div>
        </div>
        <button
          type="button"
          data-testid="scim-create-open"
          onClick={() => setDialogOpen(true)}
          className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Plus className="h-4 w-4" aria-hidden />
          New token
        </button>
      </div>

      <CopyField
        label="SCIM base URL"
        value={base}
        hint="Point your IdP's provisioning here"
      />

      {revokeError ? (
        <p role="alert" className="text-sm text-danger">
          {revokeError}
        </p>
      ) : null}

      {tokensQuery.isLoading ? (
        <TokensSkeleton />
      ) : tokensQuery.isError ? (
        <p
          role="status"
          className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground"
        >
          SCIM tokens are unavailable right now. Try again shortly.
        </p>
      ) : tokens.length === 0 ? (
        <EmptyTokens onCreate={() => setDialogOpen(true)} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[40rem] text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Token</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium">Last used</th>
                <th className="px-3 py-2 font-medium">Expires</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => {
                const status = scimTokenStatus(token);
                return (
                  <tr
                    key={token.id}
                    data-testid="scim-token-row"
                    className="border-b border-border/60 last:border-0"
                  >
                    <td className="px-3 py-2.5 font-medium text-foreground">
                      {token.name}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="font-mono text-xs text-muted-foreground">
                        {token.token_prefix}
                        {"…"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">
                      {formatDateTime(token.created_at)}
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">
                      {formatRelative(token.last_used_at)}
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">
                      {token.expires_at ? formatDateTime(token.expires_at) : "Never"}
                    </td>
                    <td className="px-3 py-2.5">
                      <span
                        className={cn(
                          "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium capitalize",
                          STATUS_STYLE[status],
                        )}
                      >
                        {status}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {status === "active" ? (
                        <button
                          type="button"
                          data-testid="scim-revoke"
                          onClick={() => onRevoke(token)}
                          disabled={revoking === token.id}
                          className="rounded-md px-2 py-1 text-xs font-medium text-danger transition-colors hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                        >
                          {revoking === token.id ? "Revoking…" : "Revoke"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <CreateTokenDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        workspaceId={workspaceId}
        client={client}
      />
    </section>
  );
}

function StatusPill({ active }: { active: number }) {
  const live = active > 0;
  return (
    <span
      data-testid="scim-status"
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        live
          ? "border-success/40 bg-success/10 text-success"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          live ? "bg-success" : "bg-muted-foreground",
        )}
        aria-hidden
      />
      {live
        ? `Provisioning active · ${active} token${active === 1 ? "" : "s"}`
        : "No active tokens"}
    </span>
  );
}

function EmptyTokens({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      data-testid="scim-empty"
      className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border px-6 py-10 text-center"
    >
      <KeyRound className="h-7 w-7 text-muted-foreground" aria-hidden />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">No SCIM tokens yet</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Issue a bearer token, then paste it into your identity provider to
          start syncing users and groups automatically.
        </p>
      </div>
      <button
        type="button"
        onClick={onCreate}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden />
        Create your first token
      </button>
    </div>
  );
}

function TokensSkeleton() {
  return (
    <div
      data-testid="scim-skeleton"
      aria-busy="true"
      className="flex flex-col gap-2 rounded-lg border border-border p-3"
    >
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <div className="h-4 w-24 animate-pulse rounded bg-muted" />
          <div className="h-4 w-20 animate-pulse rounded bg-muted/60" />
          <div className="ml-auto h-4 w-16 animate-pulse rounded-full bg-muted/60" />
        </div>
      ))}
    </div>
  );
}

interface CreateTokenDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  client: ForgeApiClient;
}

function createTokenErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to issue SCIM tokens.";
    if (error.status === 409)
      return "A token with that name already exists. Pick another name.";
  }
  return "Couldn't issue the token. Please try again.";
}

function CreateTokenDialog({
  open,
  onOpenChange,
  workspaceId,
  client,
}: CreateTokenDialogProps) {
  const nameId = useId();
  const expiryId = useId();
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ScimTokenCreated | null>(null);

  const create = useCreateScimToken(client);

  // Reset the form on each open transition (render-time adjustment).
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setName("");
      setExpiry("");
      setError(null);
      setCreated(null);
    }
  }

  const canSubmit = name.trim().length > 0 && !create.isPending;

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setError(null);
    create.mutate(
      {
        workspaceId,
        body: {
          name: name.trim(),
          expires_at: expiry ? `${expiry}T00:00:00Z` : null,
        },
      },
      {
        onSuccess: (token) => setCreated(token),
        onError: (err) => setError(createTokenErrorMessage(err)),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-display">
            <KeyRound aria-hidden className="h-5 w-5 text-primary" />
            {created ? "Copy your SCIM token" : "New SCIM token"}
          </DialogTitle>
          <DialogDescription>
            {created
              ? "This secret is shown once and never again. Store it in your IdP now."
              : "Issue a bearer token for your identity provider's SCIM connector."}
          </DialogDescription>
        </DialogHeader>

        {created ? (
          <div className="flex flex-col gap-4" data-testid="scim-created">
            <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>
                You won&apos;t be able to see this token again. Copy it before you
                close this dialog.
              </span>
            </div>
            <CopyField label={`Token · ${created.name}`} value={created.token} />
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                I&apos;ve saved it
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label htmlFor={nameId} className="text-sm font-medium">
                Token name
              </label>
              <input
                id={nameId}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Okta production"
                autoFocus
                required
                className={FIELD}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={expiryId} className="text-sm font-medium">
                Expires <span className="text-muted-foreground">(optional)</span>
              </label>
              <input
                id={expiryId}
                type="date"
                value={expiry}
                onChange={(e) => setExpiry(e.target.value)}
                className={FIELD}
              />
              <p className="text-xs text-muted-foreground">
                Leave blank for a non-expiring token.
              </p>
            </div>

            {error ? (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            ) : null}

            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!canSubmit}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <KeyRound className="h-4 w-4" aria-hidden />
                {create.isPending ? "Issuing…" : "Issue token"}
              </button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
