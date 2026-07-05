"use client";

/**
 * The walkthrough overlay: a theme-aware frosted scrim that dims the app while
 * the spotlighted stop card lifts above it, plus a keyboard-driven coach-mark
 * that narrates the current step.
 *
 * Keyboard: Esc leaves (resumable), ArrowRight/Enter advances, ArrowLeft goes
 * back. The coach-mark takes focus on each step so keys land without a mouse.
 */

import Link from "next/link";
import { ArrowLeft, ArrowRight, Check, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { useTour } from "./tour-context";
import { isLastStep } from "./tour-steps";

interface Anchor {
  top: number;
  left: number;
}

const COACH_WIDTH = 360;
const COACH_ESTIMATED_HEIGHT = 240;
const MARGIN = 16;

export function TourOverlay() {
  const {
    hydrated,
    isRunning,
    currentStep,
    stepIndex,
    stepCount,
    next,
    prev,
    dismiss,
  } = useTour();
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Anchor the coach-mark beneath the spotlighted target when it can be
  // measured; otherwise fall back to a centred position (also the jsdom path,
  // where getBoundingClientRect is zero — the tour stays fully testable).
  useLayoutEffect(() => {
    if (!isRunning) return;
    const place = () => {
      const el = document.querySelector<HTMLElement>('[data-tour-active="true"]');
      const rect = el?.getBoundingClientRect();
      if (!rect || (rect.width === 0 && rect.height === 0)) {
        setAnchor(null);
        return;
      }
      const left = Math.max(
        MARGIN,
        Math.min(
          rect.left + rect.width / 2 - COACH_WIDTH / 2,
          window.innerWidth - COACH_WIDTH - MARGIN,
        ),
      );
      const belowTop = rect.bottom + 12;
      const fitsBelow = belowTop + COACH_ESTIMATED_HEIGHT < window.innerHeight;
      const top = fitsBelow
        ? belowTop
        : Math.max(MARGIN, rect.top - 12 - COACH_ESTIMATED_HEIGHT);
      setAnchor({ top, left });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [isRunning, stepIndex]);

  // Pull focus to the coach-mark each step so the keyboard controls it.
  useEffect(() => {
    if (isRunning) dialogRef.current?.focus();
  }, [isRunning, stepIndex]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        dismiss();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        next();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        prev();
      }
    },
    [dismiss, next, prev],
  );

  if (!hydrated || !isRunning || !currentStep) return null;

  const last = isLastStep(stepIndex);
  const anchored = anchor !== null;

  return (
    <div data-testid="tour-overlay" aria-hidden={false}>
      {/* Frosted scrim — dims the app; the active stop card lifts above it. */}
      <div
        data-testid="tour-scrim"
        onClick={dismiss}
        className="fixed inset-0 z-40 bg-background/80 backdrop-blur-sm"
      />

      {/* Coach-mark */}
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="false"
        aria-label={`Walkthrough — ${currentStep.title}`}
        data-testid="tour-coachmark"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        style={
          anchored
            ? { top: anchor.top, left: anchor.left, width: COACH_WIDTH }
            : undefined
        }
        className={cn(
          "fixed z-50 flex flex-col gap-4 rounded-xl border border-border bg-popover p-5 text-popover-foreground shadow-xl outline-none",
          "focus-visible:ring-2 focus-visible:ring-ring",
          anchored
            ? ""
            : "bottom-6 left-1/2 w-[min(92vw,360px)] -translate-x-1/2",
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium uppercase tracking-wide text-primary">
              Step {stepIndex + 1} of {stepCount}
            </span>
            <h2 className="font-display text-lg font-semibold leading-tight tracking-tight">
              {currentStep.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={dismiss}
            aria-label="Leave walkthrough"
            className="-mr-1 -mt-1 rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        <p className="text-sm leading-relaxed text-muted-foreground">
          {currentStep.body}
        </p>

        {currentStep.href && currentStep.cta ? (
          <Button asChild variant="outline" size="sm" className="self-start">
            <Link href={currentStep.href} data-testid="tour-cta">
              {currentStep.cta}
              <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
          </Button>
        ) : null}

        {/* Progress dots */}
        <div
          className="flex items-center gap-1.5"
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={stepCount}
          aria-valuenow={stepIndex + 1}
          aria-label="Walkthrough progress"
        >
          {Array.from({ length: stepCount }).map((_, i) => (
            <span
              key={i}
              data-testid={i === stepIndex ? "tour-dot-active" : "tour-dot"}
              className={cn(
                "h-1.5 rounded-full transition-all",
                i === stepIndex
                  ? "w-5 bg-primary"
                  : i < stepIndex
                    ? "w-1.5 bg-primary/50"
                    : "w-1.5 bg-border",
              )}
            />
          ))}
        </div>

        <div className="flex items-center justify-between gap-2 pt-1">
          <button
            type="button"
            onClick={dismiss}
            className="rounded-md px-1 text-xs font-medium text-muted-foreground underline-offset-4 transition-colors hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Skip tour
          </button>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={prev}
              disabled={stepIndex === 0}
            >
              <ArrowLeft className="h-4 w-4" aria-hidden />
              Back
            </Button>
            <Button type="button" size="sm" onClick={next} data-testid="tour-next">
              {last ? (
                <>
                  <Check className="h-4 w-4" aria-hidden />
                  Finish
                </>
              ) : (
                <>
                  Next
                  <ArrowRight className="h-4 w-4" aria-hidden />
                </>
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
