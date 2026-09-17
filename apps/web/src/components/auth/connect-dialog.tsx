"use client";

import { KeyRound, Loader2 } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import type { TokenPersistence } from "@/lib/api/session";

export interface ConnectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the verified token once `/auth/me` accepts it. */
  onConnect: (token: string, persistence: TokenPersistence) => void;
  /** Injectable for tests. */
  client?: ForgeApiClient;
}

/**
 * The sign-in surface for a self-hosted Forge.
 *
 * Until OIDC lands, a Forge API key *is* the credential: `make dev` mints an
 * admin key and prints it, and this is where it goes. The key is verified
 * against `GET /auth/me` **before** it is stored, so a typo fails here with a
 * readable message instead of turning every screen into a silent 401.
 */
export function ConnectDialog({
  open,
  onOpenChange,
  onConnect,
  client = apiClient,
}: ConnectDialogProps) {
  const [value, setValue] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const fieldId = useId();
  const rememberId = useId();
  const errorId = useId();

  async function submit(event: FormEvent) {
    event.preventDefault();
    const token = value.trim();
    if (!token) {
      setError("Paste the API key printed by `make dev`.");
      return;
    }

    setChecking(true);
    setError(null);
    try {
      // Verify before persisting: `token` is passed per-request because the
      // client has not been told about this key yet.
      const principal = await client.me({ token });
      onConnect(token, remember ? "local" : "session");
      setValue("");
      onOpenChange(false);
      toast(`Connected as ${principal.email ?? principal.role}`, "success");
    } catch (cause) {
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
        setError("That key was rejected. It may be revoked, expired, or mistyped.");
      } else if (cause instanceof ApiError) {
        setError(`The API answered ${cause.status}. Check that the stack is running.`);
      } else {
        setError(
          `Could not reach the API at ${client.baseUrl}. Check that the stack is up.`,
        );
      }
    } finally {
      setChecking(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="connect-dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound aria-hidden className="h-4 w-4" />
            Connect to Forge
          </DialogTitle>
          <DialogDescription>
            Paste a Forge API key. <code className="font-mono">make dev</code> prints
            an admin key when it seeds the demo workspace; you can also mint one with{" "}
            <code className="font-mono">scripts/dev.sh seed</code>.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={fieldId} className="text-sm font-medium">
              API key
            </label>
            <input
              id={fieldId}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder="forge_system_…"
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              className="h-10 rounded-md border border-input bg-background px-3 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            {error ? (
              <p id={errorId} role="alert" className="text-sm text-destructive">
                {error}
              </p>
            ) : null}
          </div>

          <div className="flex items-start gap-2">
            <input
              id={rememberId}
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-input"
            />
            <label htmlFor={rememberId} className="text-sm text-muted-foreground">
              Remember on this browser
              <span className="block text-xs">
                Off by default, the key is kept only for this tab. Persisting it
                stores it where any other app served from this same origin can read
                it — which on <code className="font-mono">localhost</code> means any
                other local app that has used this port.
              </span>
            </label>
          </div>

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
              disabled={checking}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={checking}>
              {checking ? <Loader2 aria-hidden className="animate-spin" /> : null}
              {checking ? "Verifying…" : "Connect"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
