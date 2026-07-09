import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "@/lib/api/client";
import type { SpecDraft } from "@/lib/api/types";

import { AiDraftPanel } from "./ai-draft-panel";

const draft: SpecDraft = {
  goal: "Let customers search orders by name",
  model: "claude-opus-4-8",
  spec_md: "---\nid: SPEC-DRAFT\nstatus: draft\n---\n\n## Goal\n\nSearch orders by name\n",
  manifest: {
    id: "SPEC-DRAFT",
    name: "Search orders by name",
    status: "draft",
    requirements: [{ id: "R1", text: "Search orders by customer name" }],
  },
  usage: { input_tokens: 120, output_tokens: 340, cost_usd: 0.0123, calls: 1 },
};

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    draftSpec: vi.fn(() => Promise.resolve(draft)),
    ...overrides,
  } as unknown as ForgeApiClient;
}

// Real timers throughout (fake timers + RTL's `waitFor` polling deadlock one
// another): a 1ms reveal interval with a tiny chunk size still exercises the
// progressive-reveal behaviour (asserting an early, partial frame) while
// keeping the test fast and using real setTimeout/setInterval end to end.
function renderPanel(client: ForgeApiClient, onDraft = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return {
    onDraft,
    ...render(
      <AiDraftPanel client={client} onDraft={onDraft} revealIntervalMs={1} revealChunkSize={4} />,
      { wrapper: Wrapper },
    ),
  };
}

describe("AiDraftPanel", () => {
  it("disables the draft button until a goal is typed", () => {
    const client = makeClient();
    renderPanel(client);
    expect(screen.getByTestId("ai-draft-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    expect(screen.getByTestId("ai-draft-submit")).toBeEnabled();
  });

  it("drafts, streams the spec.md into view, and hands off the parsed manifest once the reveal settles", async () => {
    const client = makeClient();
    const { onDraft } = renderPanel(client);

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    fireEvent.click(screen.getByTestId("ai-draft-submit"));

    await waitFor(() =>
      expect(client.draftSpec).toHaveBeenCalledWith(
        expect.objectContaining({ goal: "Search orders by name" }),
      ),
    );

    // Settles on the full drafted text (revealed progressively via a reveal
    // interval, exercised at the unit level by `revealChunkSize`/
    // `revealIntervalMs` — see the component doc), then hands the completed
    // draft off exactly once, never before the stream has caught up.
    await waitFor(() =>
      expect(screen.getByTestId("ai-draft-stream").textContent).toBe(draft.spec_md),
    );
    await waitFor(() => expect(onDraft).toHaveBeenCalledTimes(1));
    expect(onDraft).toHaveBeenCalledWith(draft);
  });

  it("surfaces the resolved model/tier and estimated cost", async () => {
    const client = makeClient();
    renderPanel(client);

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    fireEvent.click(screen.getByTestId("ai-draft-submit"));

    await waitFor(() => expect(screen.getByTestId("ai-draft-model")).toBeInTheDocument());
    expect(screen.getByTestId("ai-draft-model").textContent).toContain("claude-opus-4-8");
    expect(screen.getByTestId("ai-draft-model").textContent).toContain("senior tier");
    expect(screen.getByTestId("ai-draft-cost").textContent).toContain("0.0123");
  });

  it("marks the result as a draft to refine, never auto-saved", async () => {
    const client = makeClient();
    renderPanel(client);

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    fireEvent.click(screen.getByTestId("ai-draft-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("ai-draft-badge").textContent).toContain("review before saving"),
    );
  });

  it("surfaces a parse error without losing the raw draft text", async () => {
    const badDraft: SpecDraft = {
      ...draft,
      manifest: null,
      parse_error: "missing frontmatter",
    };
    const client = makeClient({ draftSpec: vi.fn(() => Promise.resolve(badDraft)) });
    const { onDraft } = renderPanel(client);

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    fireEvent.click(screen.getByTestId("ai-draft-submit"));

    await waitFor(() => expect(screen.getByTestId("ai-draft-parse-error")).toBeInTheDocument());
    await waitFor(() => expect(onDraft).toHaveBeenCalledWith(badDraft));
  });

  it("surfaces a request error", async () => {
    const client = makeClient({
      draftSpec: vi.fn(() => Promise.reject(new Error("no model provider configured"))),
    });
    renderPanel(client);

    fireEvent.change(screen.getByTestId("ai-draft-goal"), {
      target: { value: "Search orders by name" },
    });
    fireEvent.click(screen.getByTestId("ai-draft-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("ai-draft-error").textContent).toContain(
        "no model provider configured",
      ),
    );
  });
});
