/** Fork: the pure part of a sub-row's reset on a project screen (handoff and playtest). */
import * as Equal from "effect/Equal";

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
