import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import type { ForgeApiClient } from "@/lib/api/client";
import type { OnboardingProgress } from "@/lib/api/types";

import { WalkthroughView } from "./walkthrough-view";
import { TOUR_STEPS } from "./tour-steps";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

function progressWith(
  overrides: Partial<Record<"spec" | "run" | "review" | "merge", number>>,
): OnboardingProgress {
  const counts = { spec: 0, run: 0, review: 0, merge: 0, ...overrides };
  const steps = (["spec", "run", "review", "merge"] as const).map((key) => ({
    key,
    count: counts[key],
    done: counts[key] > 0,
  }));
  const completedCount = steps.filter((s) => s.done).length;
  return {
    projectId: "default",
    steps,
    completedCount,
    totalCount: 4,
    allComplete: completedCount === 4,
  };
}

function makeClient(
  getOnboardingProgress: ForgeApiClient["getOnboardingProgress"],
): ForgeApiClient {
  return { getOnboardingProgress } as unknown as ForgeApiClient;
}

function renderView(props: Partial<Parameters<typeof WalkthroughView>[0]> = {}) {
  const client =
    props.client ??
    makeClient(vi.fn(() => Promise.resolve(progressWith({ spec: 1 }))));
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(
    <WalkthroughView
      client={client}
      autoStart={false}
      storageKey="test.walkthrough"
      {...props}
    />,
    { wrapper: Wrapper },
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("WalkthroughView", () => {
  it("renders the hero and the four loop stops", async () => {
    renderView();
    expect(
      screen.getByRole("heading", { name: /get started with forge/i }),
    ).toBeInTheDocument();
    for (const key of ["spec", "run", "review", "merge"]) {
      expect(screen.getByTestId(`stop-${key}`)).toBeInTheDocument();
    }
    // Each stop deep-links into the real product route.
    expect(await screen.findByTestId("stop-cta-spec")).toHaveAttribute(
      "href",
      "/specs",
    );
    expect(screen.getByTestId("stop-cta-merge")).toHaveAttribute(
      "href",
      "/deployments",
    );
  });

  it("auto-opens the tour on a first-ever visit", async () => {
    renderView({ autoStart: true });
    expect(await screen.findByTestId("tour-coachmark")).toBeInTheDocument();
    expect(screen.getByText(/step 1 of 5/i)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /the forge loop/i }),
    ).toBeInTheDocument();
  });

  it("shows a loading state while progress is in flight", () => {
    renderView({
      client: makeClient(vi.fn(() => new Promise<OnboardingProgress>(() => {}))),
    });
    expect(screen.getByTestId("progress-loading")).toBeInTheDocument();
    expect(screen.getByTestId("chip-loading-spec")).toBeInTheDocument();
  });

  it("degrades gracefully when progress fails to load", async () => {
    renderView({
      client: makeClient(vi.fn(() => Promise.reject(new Error("offline")))),
    });
    expect(await screen.findByTestId("progress-error")).toBeInTheDocument();
  });

  it("guides an empty workspace to its first action", async () => {
    renderView({
      client: makeClient(vi.fn(() => Promise.resolve(progressWith({})))),
    });
    expect(await screen.findByTestId("progress-empty")).toBeInTheDocument();
    expect(screen.getByTestId("chip-todo-spec")).toHaveTextContent(
      /not started/i,
    );
  });

  it("shows live counts and the completion banner when the loop is done", async () => {
    renderView({
      client: makeClient(
        vi.fn(() =>
          Promise.resolve(
            progressWith({ spec: 3, run: 1, review: 2, merge: 1 }),
          ),
        ),
      ),
    });
    expect(await screen.findByTestId("progress-complete")).toBeInTheDocument();
    expect(screen.getByTestId("chip-done-spec")).toHaveTextContent("3 specs");
    expect(screen.getByTestId("chip-done-merge")).toHaveTextContent(
      "1 deployment",
    );
  });

  it("starts the tour from the hero and advances / rewinds steps", async () => {
    renderView();
    await screen.findByTestId("stop-spec");

    fireEvent.click(screen.getByTestId("hero-start"));

    expect(await screen.findByTestId("tour-coachmark")).toBeInTheDocument();
    // Welcome spotlights the whole loop.
    expect(screen.getByLabelText(/the forge build loop/i)).toHaveAttribute(
      "data-tour-active",
      "true",
    );

    fireEvent.click(screen.getByTestId("tour-next"));
    expect(
      await screen.findByRole("heading", { level: 2, name: /create a spec/i }),
    ).toBeInTheDocument();
    // The spec stop is now the spotlighted element.
    expect(screen.getByTestId("stop-spec")).toHaveAttribute(
      "data-tour-active",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(
      await screen.findByRole("heading", { name: /the forge loop/i }),
    ).toBeInTheDocument();
  });

  it("advances and dismisses via the keyboard, then persists a resume point", async () => {
    renderView();
    fireEvent.click(screen.getByTestId("hero-start"));
    const coach = await screen.findByTestId("tour-coachmark");

    // ArrowRight advances to the spec step.
    fireEvent.keyDown(coach, { key: "ArrowRight" });
    expect(
      await screen.findByRole("heading", { level: 2, name: /create a spec/i }),
    ).toBeInTheDocument();

    // Escape leaves the tour (resumable).
    fireEvent.keyDown(screen.getByTestId("tour-coachmark"), { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByTestId("tour-coachmark")).not.toBeInTheDocument(),
    );

    // The hero now offers to resume from where we left off.
    expect(screen.getByTestId("hero-start")).toHaveTextContent(/resume/i);
  });

  it("resumes and restarts from the Help menu", async () => {
    renderView();
    // Advance one step then dismiss so there is something to resume.
    fireEvent.click(screen.getByTestId("hero-start"));
    fireEvent.click(await screen.findByTestId("tour-next"));
    await screen.findByRole("heading", { level: 2, name: /create a spec/i });
    fireEvent.click(screen.getByRole("button", { name: /skip tour/i }));
    await waitFor(() =>
      expect(screen.queryByTestId("tour-coachmark")).not.toBeInTheDocument(),
    );

    // Help menu → Resume picks up on the spec step.
    fireEvent.click(screen.getByTestId("help-menu-trigger"));
    fireEvent.click(await screen.findByTestId("help-start"));
    expect(
      await screen.findByRole("heading", { level: 2, name: /create a spec/i }),
    ).toBeInTheDocument();

    // Help menu → Restart returns to the welcome step.
    fireEvent.click(screen.getByTestId("help-menu-trigger"));
    fireEvent.click(await screen.findByTestId("help-restart"));
    expect(
      await screen.findByRole("heading", { name: /the forge loop/i }),
    ).toBeInTheDocument();
  });

  it("finishes the tour and offers a replay", async () => {
    // Seed a running tour parked on the final step.
    window.localStorage.setItem(
      "test.walkthrough",
      JSON.stringify({ status: "running", stepIndex: TOUR_STEPS.length - 1 }),
    );
    renderView();

    const finish = await screen.findByTestId("tour-next");
    expect(finish).toHaveTextContent(/finish/i);
    fireEvent.click(finish);

    await waitFor(() =>
      expect(screen.queryByTestId("tour-coachmark")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("hero-start")).toHaveTextContent(/replay/i);
  });

  it("exposes start + restart commands in the ⌘K palette", async () => {
    renderView();
    await screen.findByTestId("stop-spec");

    fireEvent.keyDown(document, { key: "k", metaKey: true });

    expect(
      await screen.findByText(/start product walkthrough/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/restart walkthrough from the beginning/i),
    ).toBeInTheDocument();
  });
});
