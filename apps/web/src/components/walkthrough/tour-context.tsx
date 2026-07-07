"use client";

/**
 * Guided-walkthrough tour engine — a custom, design-token-native tour (no
 * third-party dependency) so it renders in the Forge system and stays fully
 * keyboard-accessible.
 *
 * The tour is dismissible, resumable and restartable: its status + position are
 * persisted to `localStorage`, so leaving mid-tour and returning picks up where
 * you left off, and a completed tour can be replayed from the Help menu.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  TOUR_STEPS,
  clampStepIndex,
  isLastStep,
  type TourStep,
} from "./tour-steps";

export type TourStatus = "idle" | "running" | "dismissed" | "completed";

export interface TourState {
  status: TourStatus;
  stepIndex: number;
}

export interface TourContextValue extends TourState {
  /** True once the client has hydrated persisted state (overlay may render). */
  hydrated: boolean;
  isRunning: boolean;
  /** The step currently being taught, or null when the tour is not running. */
  currentStep: TourStep | null;
  /** The `data-tour` anchor to spotlight, or null when not running. */
  activeTarget: string | null;
  stepCount: number;
  /** Resume from the persisted position (or start fresh). */
  start: () => void;
  /** Restart from the very first step. */
  restart: () => void;
  next: () => void;
  prev: () => void;
  goTo: (index: number) => void;
  /** Leave the tour but remember the position for a later resume. */
  dismiss: () => void;
  /** Mark the tour complete (its position rests on the final step). */
  finish: () => void;
}

const TourContext = createContext<TourContextValue | null>(null);

export const WALKTHROUGH_STORAGE_KEY = "forge.walkthrough.v1";

function readPersisted(storageKey: string): TourState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<TourState>;
    const status: TourStatus =
      parsed.status === "running" ||
      parsed.status === "dismissed" ||
      parsed.status === "completed"
        ? parsed.status
        : "idle";
    return {
      status,
      stepIndex: clampStepIndex(Number(parsed.stepIndex ?? 0)),
    };
  } catch {
    return null;
  }
}

function writePersisted(storageKey: string, state: TourState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(state));
  } catch {
    // Storage may be unavailable (private mode, quota) — degrade silently; the
    // tour still works for the session, it just won't survive a reload.
  }
}

export interface TourProviderProps {
  children: ReactNode;
  /** Auto-open on a first-ever visit (no persisted state). Defaults to true. */
  autoStart?: boolean;
  /** Override the storage key (test isolation). */
  storageKey?: string;
}

export function TourProvider({
  children,
  autoStart = true,
  storageKey = WALKTHROUGH_STORAGE_KEY,
}: TourProviderProps) {
  // Start deterministic (idle) so server and first client render agree; the
  // real persisted/auto-start decision is applied on mount to avoid hydration
  // mismatches and SSR `localStorage` access.
  const [state, setState] = useState<TourState>({
    status: "idle",
    stepIndex: 0,
  });
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    // Post-mount hydration from localStorage (unavailable during SSR). These
    // setState calls are a deliberate external-store sync run after the first
    // client render to avoid a hydration mismatch — not a cascading in-render
    // update, so set-state-in-effect is a false positive here.
    /* eslint-disable react-hooks/set-state-in-effect */
    const persisted = readPersisted(storageKey);
    if (persisted) {
      setState(persisted);
    } else if (autoStart) {
      setState({ status: "running", stepIndex: 0 });
    }
    setHydrated(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [autoStart, storageKey]);

  useEffect(() => {
    if (!hydrated) return;
    writePersisted(storageKey, state);
  }, [hydrated, state, storageKey]);

  const start = useCallback(() => {
    setState((prev) => ({
      status: "running",
      // Resume from where a dismissed tour left off; a completed/idle tour that
      // was already at the end restarts cleanly from the top.
      stepIndex:
        prev.status === "dismissed" ? clampStepIndex(prev.stepIndex) : prev.stepIndex,
    }));
  }, []);

  const restart = useCallback(() => {
    setState({ status: "running", stepIndex: 0 });
  }, []);

  const next = useCallback(() => {
    setState((prev) => {
      if (isLastStep(prev.stepIndex)) {
        return { status: "completed", stepIndex: prev.stepIndex };
      }
      return { status: "running", stepIndex: prev.stepIndex + 1 };
    });
  }, []);

  const prev = useCallback(() => {
    setState((current) => ({
      status: "running",
      stepIndex: clampStepIndex(current.stepIndex - 1),
    }));
  }, []);

  const goTo = useCallback((index: number) => {
    setState({ status: "running", stepIndex: clampStepIndex(index) });
  }, []);

  const dismiss = useCallback(() => {
    setState((prev) => ({ status: "dismissed", stepIndex: prev.stepIndex }));
  }, []);

  const finish = useCallback(() => {
    setState({ status: "completed", stepIndex: TOUR_STEPS.length - 1 });
  }, []);

  const isRunning = state.status === "running";
  const currentStep = isRunning ? TOUR_STEPS[state.stepIndex] ?? null : null;

  const value = useMemo<TourContextValue>(
    () => ({
      ...state,
      hydrated,
      isRunning,
      currentStep,
      activeTarget: currentStep?.target ?? null,
      stepCount: TOUR_STEPS.length,
      start,
      restart,
      next,
      prev,
      goTo,
      dismiss,
      finish,
    }),
    [
      state,
      hydrated,
      isRunning,
      currentStep,
      start,
      restart,
      next,
      prev,
      goTo,
      dismiss,
      finish,
    ],
  );

  return <TourContext.Provider value={value}>{children}</TourContext.Provider>;
}

/** Access the walkthrough tour controller. Must be inside a {@link TourProvider}. */
export function useTour(): TourContextValue {
  const ctx = useContext(TourContext);
  if (!ctx) {
    throw new Error("useTour must be used within <TourProvider>");
  }
  return ctx;
}

/**
 * Whether a given `data-tour` anchor is the one currently spotlighted. Lets a
 * stop card lift itself above the scrim without any DOM measurement.
 */
export function useTourTarget(target: string): boolean {
  const { isRunning, activeTarget } = useTour();
  return isRunning && activeTarget === target;
}
