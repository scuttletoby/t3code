// Fork: settings saved before the automatic-update keys took upstream's names still say
// `autoUpdateServer` / `autoUpdateQuietCommand`. Copy them to the new names before decoding, so the
// choice loads without a schema migration; the next write saves only the new names. Delete this
// file and its call in serverSettings.ts when upstream's updater replaces ours.

const RETIRED_KEYS = [
  { retired: "autoUpdateServer", current: "automaticUpdates", type: "boolean" },
  { retired: "autoUpdateQuietCommand", current: "automaticUpdatesQuietCommand", type: "string" },
] as const;

/**
 * Copies each retired key's value to its current name. A current key already in the file wins.
 * A retired value of the wrong type is not copied: it would fail the whole file's decode, and a
 * failed decode resets every setting to its default.
 */
export function renameRetiredAutoUpdateKeys(parsed: unknown): unknown {
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return parsed;
  const stored = parsed as Readonly<Record<string, unknown>>;
  let renamed: Record<string, unknown> | undefined;
  for (const { retired, current, type } of RETIRED_KEYS) {
    const value = stored[retired];
    if (stored[current] !== undefined || typeof value !== type) continue;
    renamed = { ...(renamed ?? stored), [current]: value };
  }
  return renamed ?? parsed;
}
