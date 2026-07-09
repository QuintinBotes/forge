import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { EpicDTO, SpecDraft, SpecManifest } from "@/lib/api/types";

import { NewSpecPage } from "./new-spec-page";

const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => mockSearchParams,
}));

const epics: EpicDTO[] = [
  { id: "e1", title: "Auth overhaul" },
  { id: "e2", title: "Billing v2" },
];

const aiDraft: SpecDraft = {
  goal: "Passwordless auth",
  model: "claude-opus-4-8",
  spec_md: "---\nid: SPEC-DRAFT\nstatus: draft\n---\n\n## Goal\n\nPasswordless auth\n",
  manifest: {
    id: "SPEC-DRAFT",
    name: "Passwordless auth",
    requirements: [{ id: "R1", text: "Sign in without a password" }],
  },
  usage: { cost_usd: 0.01 },
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    listEpics: vi.fn(() => Promise.resolve(epics)),
    createEpic: vi.fn((epic: EpicDTO) =>
      Promise.resolve({ ...epic, id: "e-new" } as EpicDTO),
    ),
    createSpec: vi.fn((body: { epic_id: string; name: string }) =>
      Promise.resolve({ id: "s-new", name: body.name, status: "draft" } as SpecManifest),
    ),
    putSpecManifest: vi.fn((specId: string, manifest: SpecManifest) =>
      Promise.resolve({ ...manifest, id: specId } as SpecManifest),
    ),
    draftSpec: vi.fn(() => Promise.resolve(aiDraft)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

afterEach(() => {
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }
});

function renderPage(client: ForgeApiClient, onCreated = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return {
    onCreated,
    ...render(<NewSpecPage client={client} onCreated={onCreated} />, { wrapper: Wrapper }),
  };
}

describe("NewSpecPage", () => {
  it("lists epics to pick from and disables create until an epic + goal are set", async () => {
    const client = makeClient();
    renderPage(client);

    await screen.findByText("Auth overhaul");
    expect(screen.getByTestId("create-spec")).toBeDisabled();

    fireEvent.change(screen.getByTestId("new-spec-epic"), { target: { value: "e1" } });
    expect(screen.getByTestId("create-spec")).toBeDisabled();

    fireEvent.change(screen.getByTestId("guided-name"), { target: { value: "Passwordless auth" } });
    expect(screen.getByTestId("create-spec")).toBeEnabled();
  });

  it("creates the spec and hands off the new id", async () => {
    const client = makeClient();
    const { onCreated } = renderPage(client);
    await screen.findByText("Auth overhaul");

    fireEvent.change(screen.getByTestId("new-spec-epic"), { target: { value: "e1" } });
    fireEvent.change(screen.getByTestId("guided-name"), { target: { value: "Passwordless auth" } });
    fireEvent.click(screen.getByTestId("create-spec"));

    await waitFor(() =>
      expect(client.createSpec).toHaveBeenCalledWith(
        expect.objectContaining({ epic_id: "e1", name: "Passwordless auth" }),
      ),
    );
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("s-new"));
    // No acceptance criteria / advanced fields were drafted, so the create
    // call alone is sufficient — no follow-up PUT is needed.
    expect(client.putSpecManifest).not.toHaveBeenCalled();
  });

  it("uses the shared Guided-mode form for requirements and acceptance criteria", async () => {
    const client = makeClient();
    renderPage(client);
    await screen.findByText("Auth overhaul");
    expect(screen.getByTestId("guided-requirements")).toBeInTheDocument();
    expect(screen.getByTestId("guided-acceptance-criteria")).toBeInTheDocument();
  });

  it("persists acceptance criteria drafted before creation via a follow-up PUT", async () => {
    const client = makeClient();
    const { onCreated } = renderPage(client);
    await screen.findByText("Auth overhaul");

    fireEvent.change(screen.getByTestId("new-spec-epic"), { target: { value: "e1" } });
    fireEvent.change(screen.getByTestId("guided-name"), { target: { value: "Passwordless auth" } });

    // Draft an acceptance criterion in the Guided form before the spec exists.
    fireEvent.click(screen.getByTestId("guided-add-acceptance-criterion"));
    fireEvent.change(screen.getByTestId("ac-item-0").querySelector('[aria-label$="given"]')!, {
      target: { value: "a user" },
    });

    fireEvent.click(screen.getByTestId("create-spec"));

    await waitFor(() => expect(client.createSpec).toHaveBeenCalled());
    await waitFor(() =>
      expect(client.putSpecManifest).toHaveBeenCalledWith(
        "s-new",
        expect.objectContaining({
          acceptance_criteria: expect.arrayContaining([
            expect.objectContaining({ text: expect.stringContaining("a user") }),
          ]),
        }),
      ),
    );
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("s-new"));
  });

  describe("Draft with AI entry", () => {
    it("hides the AI panel until 'Draft with AI' is selected", async () => {
      const client = makeClient();
      renderPage(client);
      await screen.findByText("Auth overhaul");
      expect(screen.queryByTestId("ai-draft-panel")).not.toBeInTheDocument();

      fireEvent.click(screen.getByTestId("new-spec-entry-ai"));
      expect(screen.getByTestId("ai-draft-panel")).toBeInTheDocument();
    });

    it("streams a drafted spec into the Guided form", async () => {
      const client = makeClient();
      renderPage(client);
      await screen.findByText("Auth overhaul");

      fireEvent.change(screen.getByTestId("new-spec-epic"), { target: { value: "e1" } });
      fireEvent.click(screen.getByTestId("new-spec-entry-ai"));
      fireEvent.change(screen.getByTestId("ai-draft-goal"), {
        target: { value: "Passwordless auth" },
      });
      fireEvent.click(screen.getByTestId("ai-draft-submit"));

      await waitFor(() =>
        expect(client.draftSpec).toHaveBeenCalledWith(
          expect.objectContaining({ goal: "Passwordless auth" }),
        ),
      );

      // The parsed manifest preview seeds the Guided form once the AI panel's
      // live reveal has fully streamed the drafted text in.
      await waitFor(() => expect(screen.getByTestId("guided-name")).toHaveValue("Passwordless auth"));
      expect(screen.getByTestId("create-spec")).toBeEnabled();
    });
  });

  describe("starter templates", () => {
    it("seeds a requirement and acceptance criterion when a template is picked", async () => {
      const client = makeClient();
      renderPage(client);
      await screen.findByText("Auth overhaul");

      fireEvent.click(screen.getByTestId("spec-template-bugfix"));

      expect(screen.getByTestId("spec-template-bugfix")).toHaveAttribute("aria-pressed", "true");
      expect(
        screen.getByDisplayValue(/Describe the incorrect behavior/),
      ).toBeInTheDocument();
    });

    it("does not clobber requirements already drafted before picking a template", async () => {
      const client = makeClient();
      renderPage(client);
      await screen.findByText("Auth overhaul");

      fireEvent.click(screen.getByTestId("guided-add-requirement"));
      const reqInput = screen.getByLabelText(/text$/i);
      fireEvent.change(reqInput, { target: { value: "My own requirement" } });

      fireEvent.click(screen.getByTestId("spec-template-feature"));

      expect(screen.getByDisplayValue("My own requirement")).toBeInTheDocument();
      expect(
        screen.queryByDisplayValue(/Describe the new capability/),
      ).not.toBeInTheDocument();
    });
  });

  describe("epic entry", () => {
    it("preselects the epic from an ?epicId= query param (board epic 'Create spec' entry)", async () => {
      mockSearchParams.set("epicId", "e2");
      const client = makeClient();
      renderPage(client);
      await screen.findByText("Auth overhaul");

      expect(screen.getByTestId("new-spec-epic")).toHaveValue("e2");
    });

    it("creates a new epic then the spec when 'Create new epic' is chosen (standalone /specs/new entry)", async () => {
      const client = makeClient();
      const { onCreated } = renderPage(client);
      await screen.findByText("Auth overhaul");

      fireEvent.change(screen.getByTestId("new-spec-epic"), {
        target: { value: "__new_epic__" },
      });
      expect(screen.getByTestId("create-spec")).toBeDisabled();

      fireEvent.change(screen.getByTestId("new-spec-new-epic-title"), {
        target: { value: "Fresh epic" },
      });
      fireEvent.change(screen.getByTestId("guided-name"), {
        target: { value: "Passwordless auth" },
      });
      expect(screen.getByTestId("create-spec")).toBeEnabled();

      fireEvent.click(screen.getByTestId("create-spec"));

      await waitFor(() =>
        expect(client.createEpic).toHaveBeenCalledWith(
          expect.objectContaining({ title: "Fresh epic" }),
        ),
      );
      await waitFor(() =>
        expect(client.createSpec).toHaveBeenCalledWith(
          expect.objectContaining({ epic_id: "e-new", name: "Passwordless auth" }),
        ),
      );
      await waitFor(() => expect(onCreated).toHaveBeenCalledWith("s-new"));
    });
  });
});
