import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecManifest } from "@/lib/api/types";

import { SpecStudioPage } from "./spec-studio-page";

const manifest: SpecManifest = { id: "s1", name: "Passwordless auth", status: "draft" };

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getSpecManifest: vi.fn(() => Promise.resolve(manifest)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderPage(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<SpecStudioPage specId="s1" client={client} />, { wrapper: Wrapper });
}

describe("SpecStudioPage", () => {
  it("renders the spec name and defaults to the Guided-mode Spec Studio", async () => {
    renderPage(makeClient());
    expect(await screen.findByText("Passwordless auth")).toBeInTheDocument();
    expect(await screen.findByTestId("guided-mode")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to spec validation/i })).toHaveAttribute(
      "href",
      "/specs",
    );
  });
});
