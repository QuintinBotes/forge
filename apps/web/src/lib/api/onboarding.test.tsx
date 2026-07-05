import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ForgeApiClient, type ForgeApiClient as Client } from "./client";
import { useOnboardingProgress } from "./onboarding";
import type { OnboardingProgress } from "./types";

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("ForgeApiClient.getOnboardingProgress", () => {
  it("composes specs + approvals + deployments into derived progress", async () => {
    const fetchImpl = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/projects/default/specs")) {
        return Promise.resolve(
          json({
            project_id: "default",
            specs: [
              { id: "s1", name: "Auth", status: "implementing" },
              { id: "s2", name: "Billing", status: "draft" },
            ],
          }),
        );
      }
      if (url.includes("/projects/default/deployments")) {
        return Promise.resolve(
          json([
            {
              id: "d1",
              project_id: "default",
              environment_name: "production",
              repo_id: "r",
              commit_sha: "abc",
              kind: "promotion",
              state: "succeeded",
              trigger: "manual",
              initiated_by: "u",
              requested_at: "2026-01-01T00:00:00Z",
            },
          ]),
        );
      }
      if (url.includes("/approvals")) {
        return Promise.resolve(
          json([
            { id: "a1", gate_type: "pr", status: "pending", title: "PR gate" },
          ]),
        );
      }
      return Promise.resolve(json(null));
    }) as unknown as typeof fetch;

    const client = new ForgeApiClient({
      baseUrl: "http://api.test",
      fetch: fetchImpl,
    });

    const progress = await client.getOnboardingProgress("default");

    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(progress.projectId).toBe("default");
    expect(progress.steps.find((s) => s.key === "spec")).toMatchObject({
      done: true,
      count: 2,
    });
    expect(progress.steps.find((s) => s.key === "run")).toMatchObject({
      done: true,
      count: 1,
    });
    expect(progress.steps.find((s) => s.key === "review")).toMatchObject({
      done: true,
      count: 1,
    });
    expect(progress.steps.find((s) => s.key === "merge")).toMatchObject({
      done: true,
      count: 1,
    });
    expect(progress.allComplete).toBe(true);
  });
});

const CANNED: OnboardingProgress = {
  projectId: "default",
  steps: [
    { key: "spec", done: true, count: 3 },
    { key: "run", done: false, count: 0 },
    { key: "review", done: false, count: 0 },
    { key: "merge", done: false, count: 0 },
  ],
  completedCount: 1,
  totalCount: 4,
  allComplete: false,
};

function Probe({ client }: { client: Client }) {
  const { data, isLoading } = useOnboardingProgress("default", client);
  if (isLoading) return <span>loading</span>;
  return <span>completed:{data?.completedCount}</span>;
}

describe("useOnboardingProgress", () => {
  it("reads derived progress through the client", async () => {
    const client = {
      getOnboardingProgress: vi.fn(() => Promise.resolve(CANNED)),
    } as unknown as Client;

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    function Wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      );
    }

    render(<Probe client={client} />, { wrapper: Wrapper });

    expect(await screen.findByText("completed:1")).toBeInTheDocument();
    expect(client.getOnboardingProgress).toHaveBeenCalledWith("default");
  });
});
