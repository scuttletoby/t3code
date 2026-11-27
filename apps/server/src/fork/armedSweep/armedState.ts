/**
 * Fork (issue 880): the armed PR sweep's state file: followed pull requests, wakes sent (once per
 * pull request, head and reason) and wakes pending (written before the send). A bad file reads as
 * empty; one of a newer build as `newer`, never overwritten.
 */
import type { Tracked } from "./armedJudge.ts";

export const STATE_VERSION = 1;
export const STATE_FILE = "fork-armed-sweep.json";
/** A sent key of a head no longer followed is kept this long, then forgotten. */
export const SENT_KEEP_MS = 7 * 24 * 60 * 60_000;

export interface PendingWake {
  readonly commandId: string;
  readonly keys: ReadonlyArray<string>;
}

export interface SweepState {
  readonly tracked: Readonly<Record<string, Tracked>>;
  /** Wake key (armedJudge.wakeKey) to when it was sent (ms). */
  readonly sent: Readonly<Record<string, number>>;
  readonly pending: ReadonlyArray<PendingWake>;
}

export const EMPTY_STATE: SweepState = { tracked: {}, sent: {}, pending: [] };

export type ReadState =
  | { readonly type: "ok"; readonly state: SweepState }
  /** Written by a newer build: the sweep stands still rather than lose what that build knows. */
  | { readonly type: "newer"; readonly version: number }
  /** Missing (first run) or unreadable: an empty state; `warning` says which. */
  | { readonly type: "empty"; readonly warning: string | null };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isTime = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const isTimeOrNull = (value: unknown): value is number | null => value === null || isTime(value);
const isText = (value: unknown): value is string => typeof value === "string" && value.length > 0;

function trackedOf(raw: unknown): Tracked | null {
  if (!isRecord(raw)) return null;
  const { id, repository, number, url, title, head, notGreenSince, offSince } = raw;
  if (
    !isText(id) ||
    !isText(repository) ||
    typeof number !== "number" ||
    !Number.isSafeInteger(number) ||
    typeof url !== "string" ||
    typeof title !== "string" ||
    !isText(head) ||
    !isTimeOrNull(notGreenSince) ||
    !isTimeOrNull(offSince)
  ) {
    return null;
  }
  return { id, repository, number, url, title, head, notGreenSince, offSince };
}

/** The file's text (null: no file). A bad entry is dropped alone; a bad file reads as empty. */
export function readState(text: string | null): ReadState {
  if (text === null) return { type: "empty", warning: null };
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { type: "empty", warning: "the armed sweep's state file is not JSON; starting empty" };
  }
  if (!isRecord(raw) || typeof raw.version !== "number") {
    return {
      type: "empty",
      warning: "the armed sweep's state file has no version; starting empty",
    };
  }
  if (raw.version > STATE_VERSION) return { type: "newer", version: raw.version };
  if (raw.version !== STATE_VERSION) {
    return {
      type: "empty",
      warning: `the armed sweep's state file is version ${raw.version}; starting empty`,
    };
  }
  const tracked: Record<string, Tracked> = {};
  if (isRecord(raw.tracked)) {
    for (const value of Object.values(raw.tracked)) {
      const entry = trackedOf(value);
      if (entry !== null) tracked[entry.id] = entry;
    }
  }
  const sent: Record<string, number> = {};
  if (isRecord(raw.sent)) {
    for (const [key, at] of Object.entries(raw.sent)) if (isTime(at)) sent[key] = at;
  }
  const pending: PendingWake[] = [];
  if (Array.isArray(raw.pending)) {
    for (const entry of raw.pending) {
      if (isRecord(entry) && isText(entry.commandId) && Array.isArray(entry.keys)) {
        pending.push({ commandId: entry.commandId, keys: entry.keys.filter(isText) });
      }
    }
  }
  return { type: "ok", state: { tracked, sent, pending } };
}

export function writeState(state: SweepState): string {
  return `${JSON.stringify({ version: STATE_VERSION, ...state }, null, 2)}\n`;
}
