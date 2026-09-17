import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionGate, SessionMenu } from "./session-gate";
import { ForgeApiClient } from "@/lib/api/client";
import { API_TOKEN_STORAGE_KEY, writeApiToken } from "@/lib/api/session";

/** A client whose transport is a stub, so no test touches the network. */
function stubClient(fetchImpl: typeof fetch) {
  return new ForgeApiClient({ fetch: fetchImpl });
}

function wrap(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("SessionGate", () => {
  it("names the missing credential instead of rendering an unresolvable view", async () => {
    // The finding this closes: every list stayed a skeleton and the page never
    // said the requests were 401ing.
    wrap(
      <SessionGate>
        <p>board contents</p>
      </SessionGate>,
    );

    expect(await screen.findByTestId("session-gate")).toBeInTheDocument();
    expect(screen.queryByText("board contents")).not.toBeInTheDocument();
    expect(screen.getByText(/Connect to this Forge instance/)).toBeInTheDocument();
  });

  it("names the API URL it is trying to reach", async () => {
    wrap(
      <SessionGate>
        <p>board contents</p>
      </SessionGate>,
    );

    const gate = await screen.findByTestId("session-gate");
    expect(gate.textContent).toContain(new ForgeApiClient().baseUrl);
  });

  it("says so when the stored key has been revoked", async () => {
    // Re-running the seed retires the previous bootstrap key, so a browser can
    // easily be holding one the API no longer accepts. Showing empty lists
    // behind a valid-looking session is the worst of both worlds.
    writeApiToken("forge_system_revoked");
    const fetchImpl = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("{}", { status: 401 })),
    );
    wrap(
      <SessionGate client={stubClient(fetchImpl)}>
        <p>board contents</p>
      </SessionGate>,
    );

    expect(await screen.findByText(/This API key was rejected/)).toBeInTheDocument();
    expect(screen.queryByText("board contents")).not.toBeInTheDocument();
  });

  it("renders the view once a credential exists", async () => {
    writeApiToken("forge_system_abc");
    const fetchImpl = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify({ role: "admin" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    wrap(
      <SessionGate client={stubClient(fetchImpl)}>
        <p>board contents</p>
      </SessionGate>,
    );

    expect(await screen.findByText("board contents")).toBeInTheDocument();
    expect(screen.queryByTestId("session-gate")).not.toBeInTheDocument();
  });
});

describe("SessionMenu", () => {
  it("offers a way in when disconnected", async () => {
    wrap(<SessionMenu />);
    expect(await screen.findByTestId("connect-button")).toBeInTheDocument();
  });

  it("verifies the key against /auth/me before storing it", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify({ email: "admin@forge.local", role: "admin" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    wrap(<SessionMenu client={stubClient(fetchImpl)} />);

    await user.click(await screen.findByTestId("connect-button"));
    await user.type(await screen.findByLabelText("API key"), "forge_system_valid");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => {
      expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe(
        "forge_system_valid",
      );
    });

    // Verified with the pasted key, which the client does not yet know about.
    const call = fetchImpl.mock.calls[0];
    expect(String(call?.[0])).toContain("/auth/me");
    expect((call?.[1]?.headers as Record<string, string>).Authorization).toBe(
      "Bearer forge_system_valid",
    );
  });

  it("refuses a rejected key rather than storing it", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("{}", { status: 401 })),
    );
    wrap(<SessionMenu client={stubClient(fetchImpl)} />);

    await user.click(await screen.findByTestId("connect-button"));
    await user.type(await screen.findByLabelText("API key"), "forge_system_revoked");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/rejected/);
    expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not store a key when the API is unreachable", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(() =>
      Promise.reject(new TypeError("fetch failed")),
    );
    wrap(<SessionMenu client={stubClient(fetchImpl)} />);

    await user.click(await screen.findByTestId("connect-button"));
    await user.type(await screen.findByLabelText("API key"), "forge_system_abc");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach/);
    expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("clears the credential on disconnect", async () => {
    const user = userEvent.setup();
    writeApiToken("forge_system_abc", "local");
    wrap(<SessionMenu />);

    await user.click(
      await screen.findByRole("button", {
        name: "Disconnect from this Forge instance",
      }),
    );

    expect(window.localStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
    expect(await screen.findByTestId("connect-button")).toBeInTheDocument();
  });
});
