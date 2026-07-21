import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecManifest } from "@/lib/api/types";

import { SpecStudio } from "./spec-studio";

function renderStudio(client: ForgeApiClient, specId = "SPEC-1") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<SpecStudio specId={specId} client={client} />, { wrapper: Wrapper });
}

const manifest: SpecManifest = {
  id: "SPEC-1",
  name: "Passwordless auth",
  status: "draft",
  requirements: [{ id: "R1", text: "Sign in without a password" }],
  constraints: [],
};

const specMd = "---\nid: SPEC-1\n---\n\n## Goal\n\nPasswordless auth\n";
const manifestYaml = "id: SPEC-1\nname: Passwordless auth\nstatus: draft\n";

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    getSpecManifest: vi.fn(() => Promise.resolve(manifest)),
    getSpecMarkdown: vi.fn(() => Promise.resolve(specMd)),
    getSpecManifestYaml: vi.fn(() => Promise.resolve(manifestYaml)),
    putSpecManifest: vi.fn((_id: string, m: SpecManifest) => Promise.resolve(m)),
    putSpecMarkdown: vi.fn(() => Promise.resolve(manifest)),
    putSpecManifestYaml: vi.fn(() => Promise.resolve(manifest)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

describe("SpecStudio", () => {
  it("loads the manifest and defaults to Guided mode", async () => {
    renderStudio(makeClient());
    expect(await screen.findByTestId("guided-mode")).toBeInTheDocument();
    expect(screen.getByTestId("guided-name")).toHaveValue("Passwordless auth");
  });

  it("switches to Markdown mode and lazily loads spec.md", async () => {
    const client = makeClient();
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-markdown"));

    expect(await screen.findByTestId("markdown-mode")).toBeInTheDocument();
    expect(screen.getByTestId("markdown-textarea")).toHaveValue(specMd);
    expect(client.getSpecMarkdown).toHaveBeenCalledWith("SPEC-1");
  });

  it("switches to YAML mode and lazily loads manifest.yaml with live validation", async () => {
    const client = makeClient();
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-yaml"));

    expect(await screen.findByTestId("yaml-mode")).toBeInTheDocument();
    expect(screen.getByTestId("yaml-status-valid")).toBeInTheDocument();
    expect(client.getSpecManifestYaml).toHaveBeenCalledWith("SPEC-1");
  });

  it("switches to Read mode showing the rendered manifest panel", async () => {
    renderStudio(makeClient());
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-read"));

    expect(await screen.findByTestId("manifest-panel")).toBeInTheDocument();
  });

  it("preserves unsaved YAML edits when switching away and back", async () => {
    renderStudio(makeClient());
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-yaml"));
    const textarea = await screen.findByTestId("yaml-textarea");
    fireEvent.change(textarea, {
      target: { value: "id: SPEC-1\nname: Renamed via YAML\nstatus: draft\n" },
    });

    fireEvent.click(screen.getByTestId("studio-mode-guided"));
    fireEvent.click(screen.getByTestId("studio-mode-yaml"));

    expect(await screen.findByTestId("yaml-textarea")).toHaveValue(
      "id: SPEC-1\nname: Renamed via YAML\nstatus: draft\n",
    );
  });

  it("preserves unsaved Markdown edits when switching to Guided and back", async () => {
    renderStudio(makeClient());
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-markdown"));
    const textarea = await screen.findByTestId("markdown-textarea");
    fireEvent.change(textarea, {
      target: { value: "---\nid: SPEC-1\n---\n\n## Goal\n\nRenamed via Markdown\n" },
    });

    fireEvent.click(screen.getByTestId("studio-mode-guided"));
    fireEvent.click(screen.getByTestId("studio-mode-markdown"));

    expect(await screen.findByTestId("markdown-textarea")).toHaveValue(
      "---\nid: SPEC-1\n---\n\n## Goal\n\nRenamed via Markdown\n",
    );
  });

  it("saving in YAML mode invalidates the Markdown buffer so it reloads fresh, synced text", async () => {
    const client = makeClient({
      putSpecManifestYaml: vi.fn(() =>
        Promise.resolve({ ...manifest, name: "Renamed via YAML" }),
      ),
      getSpecMarkdown: vi
        .fn()
        .mockResolvedValueOnce(specMd)
        .mockResolvedValueOnce("---\nid: SPEC-1\n---\n\n## Goal\n\nRenamed via YAML\n"),
    });
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    // Visit markdown once so it's cached, then switch to yaml and save.
    fireEvent.click(screen.getByTestId("studio-mode-markdown"));
    await screen.findByTestId("markdown-textarea");
    fireEvent.click(screen.getByTestId("studio-mode-yaml"));
    const textarea = await screen.findByTestId("yaml-textarea");
    fireEvent.change(textarea, {
      target: { value: "id: SPEC-1\nname: Renamed via YAML\nstatus: draft\n" },
    });

    fireEvent.click(screen.getByTestId("yaml-save"));
    await waitFor(() => expect(client.putSpecManifestYaml).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("studio-mode-markdown"));
    await waitFor(() => expect(client.getSpecMarkdown).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("markdown-textarea")).toHaveValue(
      "---\nid: SPEC-1\n---\n\n## Goal\n\nRenamed via YAML\n",
    );
  });

  it("saves a Guided-mode edit via putSpecManifest", async () => {
    const client = makeClient();
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.change(screen.getByTestId("guided-name"), {
      target: { value: "Passwordless auth v2" },
    });
    expect(screen.getByTestId("guided-save")).toBeEnabled();
    fireEvent.click(screen.getByTestId("guided-save"));

    await waitFor(() =>
      expect(client.putSpecManifest).toHaveBeenCalledWith(
        "SPEC-1",
        expect.objectContaining({ name: "Passwordless auth v2" }),
      ),
    );
  });

  it("rejecting in Read mode posts the note to the server and renders the returned status", async () => {
    const rejected: SpecManifest = {
      ...manifest,
      status: "rejected",
      review_note: "Missing offline handling",
    };
    const client = makeClient({
      getSpecManifest: vi.fn().mockResolvedValueOnce(manifest).mockResolvedValue(rejected),
      rejectSpec: vi.fn(() => Promise.resolve(rejected)),
    });
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-read"));
    await screen.findByTestId("read-mode");
    fireEvent.click(screen.getByTestId("decision-reject"));
    const composer = screen.getByTestId("reason-composer");
    fireEvent.change(composer.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Missing offline handling" },
    });
    fireEvent.click(screen.getByTestId("confirm-decision"));

    await waitFor(() =>
      expect(client.rejectSpec).toHaveBeenCalledWith("SPEC-1", "Missing offline handling"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("read-status")).toHaveTextContent("Rejected"),
    );
    expect(screen.getByTestId("review-decision")).toHaveTextContent("Missing offline handling");
  });

  it("requesting changes in Read mode posts the note to the server", async () => {
    const changed: SpecManifest = {
      ...manifest,
      status: "changes_requested",
      review_note: "Please add a rate limit",
    };
    const client = makeClient({
      getSpecManifest: vi.fn().mockResolvedValueOnce(manifest).mockResolvedValue(changed),
      requestSpecChanges: vi.fn(() => Promise.resolve(changed)),
    });
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-read"));
    await screen.findByTestId("read-mode");
    fireEvent.click(screen.getByTestId("decision-request_changes"));
    const composer = screen.getByTestId("reason-composer");
    fireEvent.change(composer.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Please add a rate limit" },
    });
    fireEvent.click(screen.getByTestId("confirm-decision"));

    await waitFor(() =>
      expect(client.requestSpecChanges).toHaveBeenCalledWith("SPEC-1", "Please add a rate limit"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("read-status")).toHaveTextContent("Changes requested"),
    );
  });

  it("surfaces a server error when rejecting fails (no silent local state)", async () => {
    const client = makeClient({
      rejectSpec: vi.fn(() => Promise.reject(new Error("spec engine unavailable"))),
    });
    renderStudio(client);
    await screen.findByTestId("guided-mode");

    fireEvent.click(screen.getByTestId("studio-mode-read"));
    await screen.findByTestId("read-mode");
    fireEvent.click(screen.getByTestId("decision-reject"));
    fireEvent.change(
      screen.getByTestId("reason-composer").querySelector("textarea") as HTMLTextAreaElement,
      { target: { value: "nope" } },
    );
    fireEvent.click(screen.getByTestId("confirm-decision"));

    expect(await screen.findByRole("alert")).toHaveTextContent("spec engine unavailable");
    // The status must not pretend the decision persisted.
    expect(screen.getByTestId("read-status")).toHaveTextContent("Draft");
  });

  it("disables the YAML save button and surfaces errors for an invalid manifest", async () => {
    renderStudio(makeClient());
    await screen.findByTestId("guided-mode");
    fireEvent.click(screen.getByTestId("studio-mode-yaml"));
    const textarea = await screen.findByTestId("yaml-textarea");

    fireEvent.change(textarea, { target: { value: "id: SPEC-1\nname: X\nstatus: bogus\n" } });

    expect(screen.getByTestId("yaml-status-invalid")).toBeInTheDocument();
    expect(screen.getByTestId("yaml-save")).toBeDisabled();
  });
});
