import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type { IncidentView } from "@/lib/api/types";

import { DeclareIncidentDialog } from "./declare-incident-dialog";

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    declareIncident: vi.fn((body) =>
      Promise.resolve({
        id: "new-1",
        key: "INC-9",
        project_id: body.project_id,
        title: body.title,
        severity: body.severity ?? "medium",
        state: "incident_created",
        lifecycle_state: "incident_created",
        source: "manual",
        created_at: "2026-07-05T12:00:00Z",
        allowed_events: [],
      } as IncidentView),
    ),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderDialog(
  client: ForgeApiClient,
  props: Partial<React.ComponentProps<typeof DeclareIncidentDialog>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(
    <DeclareIncidentDialog
      open
      onOpenChange={props.onOpenChange ?? vi.fn()}
      defaultProjectId="proj-1"
      client={client}
      {...props}
    />,
    { wrapper: Wrapper },
  );
}

describe("DeclareIncidentDialog", () => {
  it("declares an incident with the entered title + prefilled project", async () => {
    const client = makeClient();
    const onDeclared = vi.fn();
    const onOpenChange = vi.fn();
    renderDialog(client, { onDeclared, onOpenChange });

    fireEvent.change(screen.getByLabelText(/title/i), {
      target: { value: "Checkout latency spike" },
    });
    fireEvent.click(screen.getByRole("button", { name: /declare incident/i }));

    await waitFor(() =>
      expect(client.declareIncident).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Checkout latency spike",
          project_id: "proj-1",
          severity: "medium",
        }),
      ),
    );
    await waitFor(() => expect(onDeclared).toHaveBeenCalled());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps submit disabled until a title is entered", () => {
    renderDialog(makeClient());
    expect(
      screen.getByRole("button", { name: /declare incident/i }),
    ).toBeDisabled();
  });

  it("surfaces a permission error without closing", async () => {
    const client = makeClient({
      declareIncident: vi.fn(() =>
        Promise.reject(new ApiError(403, "forbidden", null)),
      ),
    });
    const onOpenChange = vi.fn();
    renderDialog(client, { onOpenChange });

    fireEvent.change(screen.getByLabelText(/title/i), {
      target: { value: "Nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: /declare incident/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /don't have permission/i,
    );
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
