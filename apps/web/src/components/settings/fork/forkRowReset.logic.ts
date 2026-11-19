/** Fork: the pure part of a sub-row's reset on a project screen (handoff and playtest). */
import { HandoffSettings, PlaytestSettings, type ServerSettings } from "@t3tools/contracts";
import * as Equal from "effect/Equal";
import * as Schema from "effect/Schema";

/**
 * `value` with `keys` taken from `from` (a key `from` lacks is removed), or null when they already
 * match: a project's sub-row reset puts back only its own fields, the rest of the override stays.
 */
export function withFieldsFrom<T extends object>(
  value: T,
  from: T,
  keys: ReadonlyArray<keyof T>,
): T | null {
  if (keys.every((key) => Equal.equals(value[key], from[key]))) return null;
  const next: Partial<T> = { ...value };
  for (const key of keys) {
    if (from[key] === undefined) delete next[key];
    else next[key] = from[key];
  }
  return next as T;
}

/** Whether a value is one the server's save takes (its strict check, cross-field rules included). */
const IS_SAVABLE = {
  handoff: Schema.is(HandoffSettings),
  playtest: Schema.is(PlaytestSettings),
} as const;

/**
 * A project sub-row reset's value: the project's own value with the row's fields from the
 * environment's, or null when there is nothing to reset or the result is a value the server
 * refuses. Example: a project whose own playtest is on, given the environment's steps without one
 * it needs; turning the project's playtest off first brings the reset back.
 */
export function subRowResetValue<K extends "handoff" | "playtest">(
  key: K,
  shown: ServerSettings[K],
  environmentValue: ServerSettings[K],
  keys: ReadonlyArray<keyof ServerSettings[K]>,
): ServerSettings[K] | null {
  const next = withFieldsFrom(shown, environmentValue, keys);
  return next !== null && IS_SAVABLE[key](next) ? next : null;
}
