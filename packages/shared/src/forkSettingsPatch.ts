/**
 * Fork: applies the fork part of a settings patch by the rules in contracts' forkSettings.ts
 * (`FORK_PATCH_RULES`), so serverSettings.ts needs one split and one spread for every fork setting.
 */
import {
  FORK_PATCH_RULES,
  MCP_PROFILE_ALL,
  MCP_PROFILE_T3_ONLY,
  MCP_SUBAGENT_INHERIT,
  type ForkPatchRule,
  type ForkServerSettingKey,
  type ServerSettings,
  type ServerSettingsPatch,
  isBuiltInMcpProfileName,
  isDefinableMcpProfileName,
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
    // Never trimmed to `maxEntries` here: dropping an entry would quietly change what it set (a
    // thread pick's thread would fall back to a wider default). The save check refuses instead.
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

/**
 * Fork: why the server refused a save of fork settings, with the fix. The server fails the save
 * with upstream's `ServerSettingsError` (operation `normalize`, as for a patch that does not decode)
 * and this as its `cause`, so upstream's error type stays as it is; the cause's name and message
 * travel to the client with it.
 */
export class ForkSettingsWriteError extends Error {
  override readonly name = "ForkSettingsWriteError";
}

/**
 * A save that would leave a `replaceEntries` map with more than its `maxEntries`, and more than it
 * had: refused, so no entry is ever dropped to make room. A map already over (a hand edit) still
 * takes saves that do not grow it, so it never blocks an unrelated save.
 */
function maxEntriesProblem(current: ServerSettings, next: ServerSettings): string | undefined {
  for (const key of FORK_PATCH_KEYS) {
    const rule = FORK_PATCH_RULES[key];
    if (typeof rule !== "object" || !("replaceEntries" in rule)) continue;
    const max = rule.replaceEntries.maxEntries;
    if (max === undefined) continue;
    const count = (value: unknown) => (isRecord(value) ? Object.keys(value).length : 0);
    const after = count(next[key]);
    if (after > max && after > count(current[key])) {
      return `${key} would hold ${after} entries, more than the ${max} allowed: remove old picks first, then save again.`;
    }
  }
  return undefined;
}

/**
 * A profile reference a save sets anew that no thread could run with: `inherit` where only
 * `mcpSubagentProfile` takes it, or a name no profile has. A reference the save leaves as it was is
 * never refused, so an old or odd stored value does not block saving anything else (a thread that
 * uses it is refused when it starts). Empty means "not set" and is always fine.
 */
function mcpProfileRefProblem(
  next: ServerSettings,
  setting: string,
  value: string | undefined,
  before: string | undefined,
  takesInherit: boolean,
): string | undefined {
  const name = value?.trim() ?? "";
  if (name === "" || name === (before?.trim() ?? "")) return undefined;
  if (name === MCP_SUBAGENT_INHERIT) {
    return takesInherit
      ? undefined
      : `${setting} cannot be "${MCP_SUBAGENT_INHERIT}" (only a sub-agent rule takes it): pick "${MCP_PROFILE_ALL}", "${MCP_PROFILE_T3_ONLY}" or a profile name.`;
  }
  if (isBuiltInMcpProfileName(name)) return undefined;
  if (isDefinableMcpProfileName(name) && Object.hasOwn(next.mcpProfiles, name)) return undefined;
  return `${setting} names MCP profile "${name}", which is not set up: add that profile first, or pick "${MCP_PROFILE_ALL}" or "${MCP_PROFILE_T3_ONLY}".`;
}

/**
 * Why the server must refuse this save of fork settings, or `undefined`. Checks `next` (the
 * settings after the patch) against `current`, so it also sees a project's or thread's value that
 * a client sent back unchanged. Deleting a profile still in use is not refused yet (a later PR).
 */
export function forkSettingsWriteProblem(
  current: ServerSettings,
  next: ServerSettings,
): string | undefined {
  const problems: Array<string | undefined> = [
    maxEntriesProblem(current, next),
    mcpProfileRefProblem(next, "mcpProfile", next.mcpProfile, current.mcpProfile, false),
    mcpProfileRefProblem(
      next,
      "mcpSubagentProfile",
      next.mcpSubagentProfile,
      current.mcpSubagentProfile,
      true,
    ),
  ];
  for (const [threadId, pick] of Object.entries(next.mcpThreadProfiles)) {
    const before = Object.hasOwn(current.mcpThreadProfiles, threadId)
      ? current.mcpThreadProfiles[threadId]
      : undefined;
    problems.push(
      mcpProfileRefProblem(next, `The pick for thread ${threadId}`, pick, before, false),
    );
  }
  for (const [projectId, overrides] of Object.entries(next.projectSettingsOverrides)) {
    const stored = Object.hasOwn(current.projectSettingsOverrides, projectId)
      ? current.projectSettingsOverrides[
          projectId as keyof ServerSettings["projectSettingsOverrides"]
        ]
      : undefined;
    problems.push(
      mcpProfileRefProblem(
        next,
        `Project ${projectId}'s mcpProfile`,
        overrides.mcpProfile,
        stored?.mcpProfile,
        false,
      ),
      mcpProfileRefProblem(
        next,
        `Project ${projectId}'s mcpSubagentProfile`,
        overrides.mcpSubagentProfile,
        stored?.mcpSubagentProfile,
        true,
      ),
    );
  }
  return problems.find((problem) => problem !== undefined);
}
