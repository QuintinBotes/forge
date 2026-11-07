/**
 * Presentation helpers for the world view: precision-aware valid-time
 * formatting, API error surfacing, and defensive rendering of a proposal's
 * free-form payload and gate records. Pure functions, no React.
 */

import { ApiError } from "@/lib/api/client";
import type { FactOut, WorldPrecision } from "@/lib/api/types";

/**
 * The human-readable failure for an error: the API's `detail` string when
 * the server sent one (FastAPI puts the reason there — including the 403 a
 * proposer gets for deciding their own proposal), else the error message.
 */
export function errorDetail(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body;
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail !== "") {
        return detail;
      }
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) =>
            item && typeof item === "object" && "msg" in item
              ? String((item as { msg?: unknown }).msg ?? "")
              : "",
          )
          .filter((message) => message !== "");
        if (messages.length > 0) {
          return messages.join("; ");
        }
      }
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Something went wrong";
}

/** Render any payload value defensively (strings pass through, rest JSON). */
export function formatUnknown(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Format one valid-time bound honouring its precision: a year for `year`
 * (`2019`), a month for `month`, a date for `day`, and a fuller timestamp
 * below that. Bounds are read in UTC so a year-precision bound never
 * shifts into the neighbouring year for viewers behind UTC.
 */
export function formatValidBound(
  value: string | null,
  precision: WorldPrecision,
): string | null {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const year = String(date.getUTCFullYear());
  const month = pad2(date.getUTCMonth() + 1);
  const day = pad2(date.getUTCDate());
  switch (precision) {
    case "year":
      return year;
    case "month":
      return `${year}-${month}`;
    case "day":
    case "unknown":
      return `${year}-${month}-${day}`;
    case "hour":
      return `${year}-${month}-${day} ${pad2(date.getUTCHours())}:00`;
    case "minute":
      return `${year}-${month}-${day} ${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}`;
    case "second":
      return `${year}-${month}-${day} ${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`;
    default:
      return `${year}-${month}-${day}`;
  }
}

/** A fact's valid time as `from – to` ("unknown" start, "open" end). */
export function formatValidRange(fact: FactOut): string {
  const from =
    formatValidBound(fact.valid_from, fact.valid_from_precision) ?? "unknown";
  const to = formatValidBound(fact.valid_to, fact.valid_to_precision) ?? "open";
  return `${from} – ${to}`;
}

/** A recorded-at / created-at instant, in the viewer's locale. */
export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** Confidence as a whole-number percentage (`0.92` → `92%`). */
export function formatConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/** A short, recognisable form of a UUID for when no label is loaded yet. */
export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

/**
 * The statement keys a proposal payload may carry, in reading order. Only
 * keys actually present (and non-empty) are rendered — the payload is a
 * free-form record, so anything unexpected is shown after these.
 */
export const PROPOSAL_PAYLOAD_KEYS = [
  "subject",
  "subject_id",
  "phrase",
  "predicate_key",
  "object",
  "object_id",
  "value",
  "valid_from",
  "valid_to",
  "layer",
  "mood",
  "confidence",
] as const;

export interface PayloadEntry {
  key: string;
  value: string;
}

/** Flatten a proposal payload into renderable key/value rows, defensively. */
export function proposalPayloadEntries(
  payload: Record<string, unknown>,
): PayloadEntry[] {
  const entries: PayloadEntry[] = [];
  const seen = new Set<string>();
  const push = (key: string) => {
    if (seen.has(key) || !(key in payload)) {
      return;
    }
    seen.add(key);
    const value = formatUnknown(payload[key]);
    if (value !== "") {
      entries.push({ key, value });
    }
  };
  for (const key of PROPOSAL_PAYLOAD_KEYS) {
    push(key);
  }
  for (const key of Object.keys(payload).sort()) {
    push(key);
  }
  return entries;
}

/**
 * The gate's decision for a proposal, if the gate record carries one
 * (`decision` or `verdict`), with its confidence when present.
 */
export function gateSummary(gate: Record<string, unknown>): string | null {
  const decision = formatUnknown(gate.decision ?? gate.verdict);
  if (decision === "") {
    return null;
  }
  const confidence = gate.confidence;
  if (typeof confidence === "number") {
    return `${decision} (${formatConfidence(confidence)})`;
  }
  return decision;
}
