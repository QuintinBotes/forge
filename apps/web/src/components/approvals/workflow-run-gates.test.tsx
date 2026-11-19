import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ScfApiClient } from "@/lib/api/client";
import type { ApprovalSummary } from "@/lib/api/types";

import { ApprovalInbox } from "./approval-inbox";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

// A feature run passes its spec, plan and pr gates only through approvals
// (spec K section 4.7, #182): entering each gate opens one request, requested
// by the run's agent identity, and the API refuses the gate events on
// POST /workflow/runs/{id}/transition (409 gate_events_come_from_approvals).
const RUN = "7f1c2d3e-0000-4000-8000-000000000001";
const runGates: ApprovalSummary[] = (
  [
    ["g-spec", "spec"],
    ["g-plan", "plan"],
    ["g-pr", "pr"],
  ] as const
).map(([id, gate_type]) => ({
  id,
  gate_type,
  status: "pending",
  title: `gated feature (${gate_type})`,
  risk_level: "info",
  requested_actor: `agent:${RUN}`,
  requested_at: "2026-11-15T10:00:00Z",
}));

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The real client over a recorded fetch: every request the inbox makes. */
function recordingClient(calls: Call[]): ScfApiClient {
  const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = new URL(String(input)).pathname;
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null });
    const id = path.split("/")[2];
    if (method === "POST" && path.endsWith("/decision")) {
      return Promise.resolve(
        json({ approval_id: id, status: "approved", outcome: { completed: true } }),
      );
    }
    if (path === "/approvals") return Promise.resolve(json(runGates));
    if (path.endsWith("/context")) {
      return Promise.resolve(
        json({
          approval_id: id,
          gate_type: runGates.find((g) => g.id === id)?.gate_type,
          goal: "gated feature",
          risk_flags: [],
          run_trace_ref: { workflow_run_id: RUN },
          available_actions: ["approve", "reject", "request_changes", "escalate"],
        }),
      );
    }
    if (path.endsWith("/decisions")) return Promise.resolve(json([]));
    return Promise.resolve(json({ detail: "not found" }, 404));
  }) as unknown as typeof fetch;
  return new ScfApiClient({ baseUrl: "http://api.test", token: "t", fetch: fetchImpl });
}

function renderInbox(client: ScfApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(<ApprovalInbox client={client} />, { wrapper: Wrapper });
}

const SRC = join(__dirname, "..", "..");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("feature-run gates in the approvals inbox", () => {
  it("lists each gate a run opened, labelled by gate and requested by the run's agent", async () => {
    renderInbox(recordingClient([]));

    for (const [id, label] of [
      ["g-spec", "Spec"],
      ["g-plan", "Plan"],
      ["g-pr", "Pull request"],
    ]) {
      const row = await screen.findByTestId(`approval-row-${id}`);
      expect(within(row).getByText(label)).toBeInTheDocument();
      expect(within(row).getByText("Agent 7f1c2d3e")).toBeInTheDocument();
    }
    expect(screen.getByTestId("pending-count")).toHaveTextContent("3 pending");
  });

  it("passes a gate only by posting the decision to its approval request", async () => {
    const calls: Call[] = [];
    renderInbox(recordingClient(calls));
    await screen.findByTestId("review-panel");

    fireEvent.keyDown(screen.getByTestId("approval-inbox"), { key: "a" });

    await waitFor(() =>
      expect(calls.filter((c) => c.method !== "GET")).toEqual([
        {
          method: "POST",
          path: "/approvals/g-spec/decision",
          body: { decision: "approve", note: null },
        },
      ]),
    );
    expect(calls.some((c) => c.path.includes("/transition"))).toBe(false);
  });

  it("no web surface calls the workflow transition route", () => {
    // The gate events (GATE_EVENTS in apps/api/scf_api/services/workflow_gate_hook.py)
    // come only from approvals. A surface that needs /transition must first show
    // it never sends one of them; then narrow this check.
    const offenders = sourceFiles(SRC).filter((file) =>
      /\/transition\b/.test(readFileSync(file, "utf8")),
    );
    expect(offenders).toEqual([]);
  });
});
