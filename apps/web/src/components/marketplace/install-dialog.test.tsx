import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type { InstallPlan, Listing } from "@/lib/api/types";

import { InstallDialog } from "./install-dialog";

const listing: Listing = {
  id: "l1",
  registry_id: "11111111-1111-1111-1111-111111111111",
  registry_slug: "forge-official",
  trust_level: "official",
  kind: "skill_profile",
  slug: "python-pro",
  name: "Python Pro",
  summary: "Expert Python engineering profile",
  tags: ["python"],
  latest_version: "1.2.0",
  license: "Apache-2.0",
  cached_at: "2026-01-01T00:00:00Z",
};

function plan(over: Partial<InstallPlan> = {}): InstallPlan {
  return {
    registry_id: listing.registry_id,
    kind: "skill_profile",
    slug: "python-pro",
    version: "1.2.0",
    verification: {
      status: "verified",
      content_hash_ok: true,
      signature_ok: true,
    },
    resolved_config: { content_hash: `sha256:${"a".repeat(64)}` },
    warnings: [],
    requires_admin_followup: [],
    overrides_builtin: false,
    blocked: false,
    block_reason: null,
    ...over,
  };
}

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    previewInstall: vi.fn(() => Promise.resolve(plan())),
    installPackage: vi.fn(() =>
      Promise.resolve({
        installation_id: "inst-1",
        target_kind: "skill_profile",
        target_object_id: "obj-1",
        status: "installed" as const,
        version: "1.2.0",
        verification: { status: "verified" as const, content_hash_ok: true },
        warnings: [],
      }),
    ),
    ...over,
  } as unknown as ForgeApiClient;
}

function renderDialog(client: ForgeApiClient, onOpenChange = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  render(
    <InstallDialog
      open
      onOpenChange={onOpenChange}
      listing={listing}
      client={client}
    />,
    { wrapper: Wrapper },
  );
  return { onOpenChange };
}

describe("InstallDialog", () => {
  it("previews on open and installs a verified package (one ember action)", async () => {
    const client = makeClient();
    const { onOpenChange } = renderDialog(client);

    // The dry-run fires with the resolved (latest) version.
    await waitFor(() =>
      expect(client.previewInstall).toHaveBeenCalledWith(
        expect.objectContaining({ slug: "python-pro", version: "1.2.0" }),
      ),
    );

    expect(await screen.findByTestId("verification-verified")).toBeInTheDocument();
    const confirm = screen.getByTestId("confirm-install");
    expect(confirm).not.toBeDisabled();

    fireEvent.click(confirm);

    await waitFor(() =>
      expect(client.installPackage).toHaveBeenCalledWith(
        expect.objectContaining({
          slug: "python-pro",
          acknowledge_unverified: false,
        }),
      ),
    );
    // Success closes the dialog.
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("gates an unsigned package behind an explicit acknowledgement", async () => {
    const client = makeClient({
      previewInstall: vi.fn(() =>
        Promise.resolve(
          plan({
            verification: { status: "unsigned", content_hash_ok: true },
            warnings: ["This package is unsigned."],
          }),
        ),
      ),
    });
    renderDialog(client);

    await screen.findByTestId("verification-unsigned");
    expect(screen.getByTestId("plan-warnings")).toHaveTextContent(/unsigned/i);

    // Install is disarmed until the admin acknowledges.
    expect(screen.getByTestId("confirm-install")).toBeDisabled();

    fireEvent.click(screen.getByLabelText("Acknowledge unverified package"));
    expect(screen.getByTestId("confirm-install")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("confirm-install"));
    await waitFor(() =>
      expect(client.installPackage).toHaveBeenCalledWith(
        expect.objectContaining({ acknowledge_unverified: true }),
      ),
    );
  });

  it("hard-blocks an install when the plan is blocked", async () => {
    const client = makeClient({
      previewInstall: vi.fn(() =>
        Promise.resolve(
          plan({
            verification: { status: "hash_mismatch", content_hash_ok: false },
            blocked: true,
            block_reason: "Content hash does not match the manifest.",
          }),
        ),
      ),
    });
    renderDialog(client);

    expect(await screen.findByTestId("install-blocked")).toHaveTextContent(
      /content hash does not match/i,
    );
    expect(screen.getByTestId("confirm-install")).toBeDisabled();
    expect(client.installPackage).not.toHaveBeenCalled();
  });

  it("surfaces a 422 block reason from the preview error", async () => {
    const client = makeClient({
      previewInstall: vi.fn(() =>
        Promise.reject(
          new ApiError(422, "blocked", {
            detail: { error_code: "signature_invalid", message: "Bad signature." },
          }),
        ),
      ),
    });
    renderDialog(client);

    expect(await screen.findByTestId("install-blocked")).toHaveTextContent(
      /bad signature/i,
    );
    expect(screen.getByTestId("confirm-install")).toBeDisabled();
  });

  it("shows the assaying state while the preview is in flight", async () => {
    const client = makeClient({
      previewInstall: vi.fn(() => new Promise<InstallPlan>(() => {})),
    });
    renderDialog(client);
    expect(await screen.findByTestId("preview-loading")).toBeInTheDocument();
  });

  it("lists required admin follow-up after install", async () => {
    const client = makeClient({
      previewInstall: vi.fn(() =>
        Promise.resolve(
          plan({ requires_admin_followup: ["Provide the API key in the vault."] }),
        ),
      ),
    });
    renderDialog(client);
    expect(await screen.findByTestId("plan-followup")).toHaveTextContent(
      /api key in the vault/i,
    );
  });
});
