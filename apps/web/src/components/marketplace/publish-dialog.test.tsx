import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import { toast } from "@/components/ui/toast";
import type { ListingDetail, Registry } from "@/lib/api/types";

import { PublishDialog } from "./publish-dialog";

const REGISTRIES: Registry[] = [
  {
    id: "reg-official",
    slug: "official",
    name: "Forge Official",
    type: "http_index",
    url: "https://official.example/index.json",
    trust_level: "official",
    enabled: true,
    has_public_key: true,
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "reg-acme",
    slug: "acme-community",
    name: "Acme Community",
    type: "http_index",
    url: "https://registry.acme.test/index.json",
    trust_level: "community",
    enabled: true,
    has_public_key: false,
    created_at: "2026-01-01T00:00:00Z",
  },
];

const PUBLISHED: ListingDetail = {
  id: "l1",
  registry_id: "reg-acme",
  registry_slug: "acme-community",
  trust_level: "community",
  kind: "skill_profile",
  slug: "self-authored",
  name: "Self Authored",
  summary: "A workspace-authored skill profile.",
  tags: [],
  latest_version: "1.0.0",
  license: "Apache-2.0",
  cached_at: "2026-01-01T00:00:00Z",
  versions: [
    {
      version: "1.0.0",
      content_hash: `sha256:${"a".repeat(64)}`,
      signed: false,
      published_at: "2026-01-01T00:00:00Z",
    },
  ],
};

function makeClient(over: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listRegistries: vi.fn(() => Promise.resolve(REGISTRIES)),
    publishListing: vi.fn(() => Promise.resolve(PUBLISHED)),
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
  render(<PublishDialog open onOpenChange={onOpenChange} client={client} />, {
    wrapper: Wrapper,
  });
  return { onOpenChange };
}

async function fillMinimalForm() {
  await screen.findByRole("option", { name: "acme-community" });
  fireEvent.change(screen.getByLabelText("Slug"), {
    target: { value: "self-authored" },
  });
  fireEvent.change(screen.getByLabelText("Name"), {
    target: { value: "Self Authored" },
  });
  fireEvent.change(screen.getByLabelText("Summary"), {
    target: { value: "A workspace-authored skill profile." },
  });
  fireEvent.change(screen.getByLabelText("Artifact JSON"), {
    target: { value: '{"name": "self-authored"}' },
  });
}

describe("PublishDialog", () => {
  it("excludes the read-only official registry from the picker", async () => {
    renderDialog(makeClient());
    await screen.findByRole("option", { name: "acme-community" });
    expect(
      screen.queryByRole("option", { name: "official" }),
    ).not.toBeInTheDocument();
  });

  it("publishes a package and shows a success toast", async () => {
    const client = makeClient();
    const successSpy = vi.spyOn(toast, "success");
    const { onOpenChange } = renderDialog(client);
    await fillMinimalForm();

    fireEvent.click(screen.getByTestId("confirm-publish"));

    await waitFor(() =>
      expect(client.publishListing).toHaveBeenCalledWith(
        expect.objectContaining({
          registry_id: "reg-acme",
          kind: "skill_profile",
          slug: "self-authored",
          name: "Self Authored",
          artifact: { name: "self-authored" },
        }),
      ),
    );
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(successSpy).toHaveBeenCalledWith(
      expect.stringMatching(/published self authored/i),
    );
  });

  it("rejects invalid JSON in the artifact field before submitting", async () => {
    const client = makeClient();
    renderDialog(client);
    await fillMinimalForm();
    fireEvent.change(screen.getByLabelText("Artifact JSON"), {
      target: { value: "{not valid json" },
    });

    fireEvent.click(screen.getByTestId("confirm-publish"));

    expect(await screen.findByText(/must be valid json/i)).toBeInTheDocument();
    expect(client.publishListing).not.toHaveBeenCalled();
  });

  it("surfaces a validation-rejection error from the server", async () => {
    const client = makeClient({
      publishListing: vi.fn(() =>
        Promise.reject(
          new ApiError(422, "unprocessable", {
            detail: "skill_profile artifact failed F11 validation",
          }),
        ),
      ),
    });
    renderDialog(client);
    await fillMinimalForm();

    fireEvent.click(screen.getByTestId("confirm-publish"));

    expect(await screen.findByTestId("publish-error")).toHaveTextContent(
      /failed f11 validation/i,
    );
  });

  it("shows a guard message when there is no registry to publish into", async () => {
    renderDialog(
      makeClient({
        listRegistries: vi.fn(() =>
          Promise.resolve([REGISTRIES[0]]), // only the official registry
        ),
      }),
    );
    expect(await screen.findByTestId("publish-no-registries")).toBeInTheDocument();
    expect(screen.getByTestId("confirm-publish")).toBeDisabled();
  });

  it("switching kind swaps in a matching artifact template", async () => {
    renderDialog(makeClient());
    await screen.findByRole("option", { name: "acme-community" });

    fireEvent.change(screen.getByLabelText(/^Kind$/), {
      target: { value: "mcp_connector" },
    });

    const artifactField = screen.getByLabelText(
      "Artifact JSON",
    ) as HTMLTextAreaElement;
    expect(artifactField.value).toContain("transport");
  });
});
