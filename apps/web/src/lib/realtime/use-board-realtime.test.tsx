import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { WebSocketLike } from "./use-board-realtime";
import { useBoardRealtime } from "./use-board-realtime";

/** A controllable fake WebSocket usable from jsdom tests. */
function makeFakeSocket() {
  const listeners = new Map<string, Set<(ev: unknown) => void>>();
  const socket: WebSocketLike & {
    emit: (type: string, ev: unknown) => void;
    close: ReturnType<typeof vi.fn>;
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
    close: vi.fn(),
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
});
