/**
 * Fork: applies the fork part of a settings patch by the rules in contracts' forkSettings.ts
 * (`FORK_PATCH_RULES`), so serverSettings.ts needs one split and one spread for every fork setting.
 */
import {
  FORK_PATCH_RULES,
  type ForkPatchRule,
  type ForkServerSettingKey,
  type ServerSettings,
  type ServerSettingsPatch,
} from "@t3tools/contracts";

const FORK_PATCH_KEYS = Object.keys(FORK_PATCH_RULES) as ReadonlyArray<ForkServerSettingKey>;

export type ForkSettingsPatch = Pick<ServerSettingsPatch, ForkServerSettingKey>;

/** Splits a patch into its fork fields and the rest (upstream's), which keeps every other key. */
export function splitForkSettingsPatch<P extends ServerSettingsPatch>(
  patch: P,
): readonly [ForkSettingsPatch, Omit<P, ForkServerSettingKey>] {
  const fork: Record<string, unknown> = {};
  const rest: Record<string, unknown> = { ...patch };
  for (const key of FORK_PATCH_KEYS) {
    if (!Object.hasOwn(rest, key)) continue;
    fork[key] = rest[key];
    delete rest[key];
  }
  return [fork as ForkSettingsPatch, rest as Omit<P, ForkServerSettingKey>];
}

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function applyRule(rule: ForkPatchRule, stored: unknown, patch: unknown): unknown {
  if (rule === "replace") return patch;
  if (rule === "merge-fields") {
    const next: Record<string, unknown> = isRecord(stored) ? { ...stored } : {};
    for (const [field, value] of Object.entries(patch as Record<string, unknown>)) {
      if (value !== undefined) next[field] = value;
    }
    return next;
  }
  const next = new Map(Object.entries(isRecord(stored) ? stored : {}));
  if ("replaceEntries" in rule) {
    for (const [id, entry] of Object.entries(patch as Record<string, unknown>)) {
      if (entry === undefined) continue;
      // Delete first, so a changed entry moves to the end: the newest.
      next.delete(id);
      if (entry !== null) next.set(id, entry);
    }
    const { keepNewest } = rule.replaceEntries;
    const ids = [...next.keys()];
    if (keepNewest !== undefined && ids.length > keepNewest) {
      for (const id of ids.slice(0, ids.length - keepNewest)) next.delete(id);
    }
    return Object.fromEntries(next);
  }
  for (const [id, entry] of Object.entries(patch as Record<string, unknown>)) {
    if (entry === null) {
      next.delete(id);
    } else if (entry !== undefined) {
      const storedEntry = next.get(id);
      next.set(id, {
        ...rule.mergeEntries,
        ...(isRecord(storedEntry) ? storedEntry : {}),
        ...(entry as Record<string, unknown>),
      });
    }
  }
  return Object.fromEntries(next);
}

/** The fork settings a patch changes, each by its rule; a field the patch leaves out is absent. */
export function applyForkSettingsPatch(
  current: ServerSettings,
  patch: ForkSettingsPatch,
): Partial<Pick<ServerSettings, ForkServerSettingKey>> {
  const next: Record<string, unknown> = {};
  for (const key of FORK_PATCH_KEYS) {
    const value = patch[key];
    if (value === undefined) continue;
    next[key] = applyRule(FORK_PATCH_RULES[key], current[key], value);
  }
  return next as Partial<Pick<ServerSettings, ForkServerSettingKey>>;
}
