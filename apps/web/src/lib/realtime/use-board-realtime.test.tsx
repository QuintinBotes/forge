import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { BoardRealtimeStatus } from "./board-realtime-status";
import { BoardConnectionIndicator } from "./board-presence";
import type { WebSocketLike } from "./use-board-realtime";
import { useBoardRealtime } from "./use-board-realtime";

// `useBoardRealtime` defaults its `token` option to `apiClient.token` — mock
// the singleton so tests can assert the WS URL picks it up without depending
// on real env vars / REST auth wiring.
vi.mock("@/lib/api/client", () => ({
  apiClient: { token: "singleton-token" },
}));

/** A controllable fake WebSocket usable from jsdom tests. */
function makeFakeSocket() {
  const listeners = new Map<string, Set<(ev: unknown) => void>>();
  const socket: WebSocketLike & {
    emit: (type: string, ev: unknown) => void;
    close: ReturnType<typeof vi.fn<() => void>>;
  } = {
    readyState: 1,
    addEventListener(type, listener) {
      const set = listeners.get(type) ?? new Set();
      set.add(listener);
      listeners.set(type, set);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    close: vi.fn<() => void>(),
    emit(type, ev) {
      listeners.get(type)?.forEach((l) => l(ev));
    },
  };
  return socket;
}

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useBoardRealtime", () => {
  it("invalidates task queries when a task event arrives", () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const socket = makeFakeSocket();

    renderHook(
      () => useBoardRealtime({ socketFactory: () => socket }),
      { wrapper: makeWrapper(queryClient) },
    );

    act(() => {
      socket.emit("open", {});
      socket.emit("message", {
        data: JSON.stringify({ type: "task.updated", task_id: "t1" }),
      });
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("invalidates run queries when a run.* event arrives (RT-7)", () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const socket = makeFakeSocket();

    renderHook(
      () => useBoardRealtime({ socketFactory: () => socket }),
      { wrapper: makeWrapper(queryClient) },
    );

    act(() => {
      socket.emit("open", {});
      socket.emit("message", {
        data: JSON.stringify({ type: "run.completed", run_id: "r1" }),
      });
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["runs"] });
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("invalidates approval queries when an approval.* event arrives (RT-7)", () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const socket = makeFakeSocket();

    renderHook(
      () => useBoardRealtime({ socketFactory: () => socket }),
      { wrapper: makeWrapper(queryClient) },
    );

    act(() => {
      socket.emit("open", {});
      socket.emit("message", {
        data: JSON.stringify({ type: "approval.decided", approval_id: "a1" }),
      });
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["approvals"] });
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("forwards parsed events to onEvent and tracks connection state", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();
    const onEvent = vi.fn();

    const { result } = renderHook(
      () => useBoardRealtime({ socketFactory: () => socket, onEvent }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(result.current.connected).toBe(false);

    act(() => {
      socket.emit("open", {});
    });
    expect(result.current.connected).toBe(true);

    act(() => {
      socket.emit("message", {
        data: JSON.stringify({ type: "incident.created", incident_id: "i1" }),
      });
    });
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: "incident.created" }),
    );
  });

  it("ignores malformed payloads without throwing", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();

    renderHook(() => useBoardRealtime({ socketFactory: () => socket }), {
      wrapper: makeWrapper(queryClient),
    });

    expect(() => {
      act(() => {
        socket.emit("message", { data: "not json {{{" });
      });
    }).not.toThrow();
  });

  it("does not connect when disabled", () => {
    const queryClient = new QueryClient();
    const factory = vi.fn(() => makeFakeSocket());

    renderHook(
      () => useBoardRealtime({ enabled: false, socketFactory: factory }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(factory).not.toHaveBeenCalled();
  });

  it("closes the socket on unmount", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();

    const { unmount } = renderHook(
      () => useBoardRealtime({ socketFactory: () => socket }),
      { wrapper: makeWrapper(queryClient) },
    );

    unmount();
    expect(socket.close).toHaveBeenCalled();
  });

  it("appends the apiClient's token as ?token= on the WS URL by default", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();
    const factory = vi.fn(() => socket);

    renderHook(
      () =>
        useBoardRealtime({ url: "ws://localhost:8000/ws", socketFactory: factory }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(factory).toHaveBeenCalledWith(
      "ws://localhost:8000/ws?token=singleton-token",
    );
  });

  it("lets an explicit token option override the apiClient default", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();
    const factory = vi.fn(() => socket);

    renderHook(
      () =>
        useBoardRealtime({
          url: "ws://localhost:8000/ws?existing=1",
          token: "explicit-token",
          socketFactory: factory,
        }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(factory).toHaveBeenCalledWith(
      "ws://localhost:8000/ws?existing=1&token=explicit-token",
    );
  });

  it("omits the token param entirely when no token is available", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();
    const factory = vi.fn(() => socket);

    renderHook(
      () =>
        useBoardRealtime({
          url: "ws://localhost:8000/ws",
          token: "",
          socketFactory: factory,
        }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(factory).toHaveBeenCalledWith("ws://localhost:8000/ws");
  });
});

describe("BoardConnectionIndicator", () => {
  it("renders a presence dot and Live/Offline label from connection state", () => {
    const { rerender } = render(<BoardConnectionIndicator connected={false} />);
    expect(screen.getByTestId("board-connection-indicator")).toBeInTheDocument();
    expect(screen.getByTestId("board-connection-status")).toHaveTextContent("Offline");

    rerender(<BoardConnectionIndicator connected={true} />);
    expect(screen.getByTestId("board-connection-status")).toHaveTextContent("Live");
  });
});

describe("BoardRealtimeStatus", () => {
  it("surfaces connection state and fires a toast when an event arrives", () => {
    const queryClient = new QueryClient();
    const socket = makeFakeSocket();
    const onEvent = vi.fn();

    render(
      <BoardRealtimeStatus socketFactory={() => socket} onEvent={onEvent} />,
      { wrapper: makeWrapper(queryClient) },
    );

    expect(screen.getByTestId("board-connection-status")).toHaveTextContent("Offline");
    expect(screen.queryByTestId("board-event-toast")).not.toBeInTheDocument();

    act(() => {
      socket.emit("open", {});
      socket.emit("message", {
        data: JSON.stringify({ type: "task.updated", task_id: "t1" }),
      });
    });

    expect(screen.getByTestId("board-connection-status")).toHaveTextContent("Live");
    expect(screen.getByTestId("board-event-toast")).toHaveTextContent("Task updated");
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: "task.updated" }),
    );
  });
});
