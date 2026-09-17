"use client";

import { KeyRound, LogOut, ShieldCheck } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ConnectDialog } from "@/components/auth/connect-dialog";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { useCurrentUser } from "@/lib/api/hooks";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { maskApiToken } from "@/lib/api/session";
import { useApiSession } from "@/lib/api/use-api-session";

/**
 * Who the current key resolves to.
 *
 * Split out of {@link SessionMenu} so `/auth/me` is only ever queried once a
 * credential exists — an unauthenticated visitor should not be firing a request
 * that can only 401, and the query hook must not run before there is anything
 * for it to ask about.
 */
function ConnectedIdentity({
  fallback,
  client,
}: {
  fallback: string;
  client: ForgeApiClient;
}) {
  const me = useCurrentUser(client);
  return <>{me.data?.email ?? me.data?.role ?? fallback}</>;
}

/**
 * Top-bar session control: who you are connected as, and the way in when you
 * are not. Rendered on every screen, because there is no separate login route —
 * on a self-hosted Forge the API key *is* the session.
 */
export function SessionMenu({ client = apiClient }: { client?: ForgeApiClient } = {}) {
  const { token, ready, signIn, signOut } = useApiSession();
  const [open, setOpen] = useState(false);

  if (!ready) {
    // Pre-hydration: render nothing rather than flashing "Connect" at someone
    // who is already signed in.
    return null;
  }

  if (!token) {
    return (
      <>
        <Button size="sm" onClick={() => setOpen(true)} data-testid="connect-button">
          <KeyRound aria-hidden />
          Connect
        </Button>
        <ConnectDialog
          open={open}
          onOpenChange={setOpen}
          onConnect={signIn}
          client={client}
        />
      </>
    );
  }

  return (
    <div className="flex items-center gap-2" data-testid="session-menu">
      <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex">
        <ShieldCheck aria-hidden className="h-3.5 w-3.5" />
        <ConnectedIdentity fallback={maskApiToken(token)} client={client} />
      </span>
      <Button
        size="sm"
        variant="ghost"
        onClick={signOut}
        aria-label="Disconnect from this Forge instance"
      >
        <LogOut aria-hidden />
        <span className="hidden sm:inline">Disconnect</span>
      </Button>
    </div>
  );
}

/**
 * Renders `children` once this browser holds an API key, and an explanation of
 * how to get one when it does not.
 *
 * Without this, an unauthenticated visitor gets the full navigation over a wall
 * of skeletons that never resolve — every request is a 401 and nothing on the
 * page says so. Naming the missing credential is the difference between "this
 * product is broken" and "I have not signed in yet".
 */
export function SessionGate({
  children,
  client = apiClient,
}: {
  children: ReactNode;
  client?: ForgeApiClient;
}) {
  const { token, ready, signIn } = useApiSession();
  const [open, setOpen] = useState(false);
  // Only meaningful once a token exists; `useCurrentUser` is cheap and cached,
  // and the gate needs to know whether that token is still good.
  const me = useCurrentUser(client);
  const rejected =
    token !== undefined &&
    me.isError &&
    me.error instanceof ApiError &&
    (me.error.status === 401 || me.error.status === 403);

  if (!ready) {
    return null;
  }

  if (token && !rejected) {
    return <>{children}</>;
  }

  return (
    <>
      <EmptyState
        data-testid="session-gate"
        icon={<KeyRound aria-hidden />}
        title={rejected ? "This API key was rejected" : "Connect to this Forge instance"}
        description={
          rejected ? (
            <>
              The stored key is no longer valid — it may have been revoked or
              expired. Re-running the seed retires the previous key, so a fresh{" "}
              <code className="font-mono">scripts/dev.sh seed</code> prints the one
              to paste here.
            </>
          ) : (
            <>
              Every screen needs an API key. <code className="font-mono">make dev</code>{" "}
              prints an admin key when it seeds the demo workspace — paste it here.
              Reaching the API at <code className="font-mono">{client.baseUrl}</code>.
            </>
          )
        }
        action={
          <Button onClick={() => setOpen(true)} data-testid="session-gate-connect">
            <KeyRound aria-hidden />
            {rejected ? "Use a different key" : "Connect"}
          </Button>
        }
      />
      <ConnectDialog
        open={open}
        onOpenChange={setOpen}
        onConnect={signIn}
        client={client}
      />
    </>
  );
}
